# shellcheck shell=bash disable=SC2034
# Shared helpers for the docker-publish action scripts. Source, don't execute.

log() { echo "[docker-publish] $*" >&2; }
die() { echo "::error::$*" >&2; exit 1; }
# Registry-supplied values (image labels) may contain newlines; flatten them so a
# logged value can never start a new line, which the runner would parse as a ::command::.
oneline() { printf '%s' "$1" | tr '\r\n' '  ' | tr -d '\000-\010\013\014\016-\037'; }

# The labels input, one trimmed key=value per line, then defaults for keys no
# line sets:
#   com.tenstorrent.git.ref     GITHUB_REF, the ref that ran the workflow
#   com.tenstorrent.git.branch  the pull request's head branch, or the branch
#                               that ran the workflow (none for a tag)
#   org.opencontainers.image.revision  GITHUB_SHA, on a build of main only: a
#       pull request's GITHUB_SHA is a temporary merge commit, and a checkout
#       with `ref:` builds some other commit, so elsewhere the caller sets it.
effective_labels() {  # <labels input>
  local line keys=" " branch="${GITHUB_HEAD_REF:-}" defaults=()
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || continue
    [[ "$line" == ?*=* ]] || die "labels: '$(oneline "$line")' is not key=value."
    keys+="${line%%=*} "
    printf '%s\n' "$line"
  done <<< "$1"
  [ -n "$branch" ] || [[ "${GITHUB_REF:-}" != refs/heads/* ]] || branch="${GITHUB_REF#refs/heads/}"
  [ -z "${GITHUB_REF:-}" ] || defaults+=("com.tenstorrent.git.ref=${GITHUB_REF}")
  [ -z "$branch" ] || defaults+=("com.tenstorrent.git.branch=${branch}")
  [ "${GITHUB_REF:-}" != refs/heads/main ] || [ -z "${GITHUB_SHA:-}" ] \
    || defaults+=("org.opencontainers.image.revision=${GITHUB_SHA}")
  for line in "${defaults[@]}"; do
    [[ "$keys" == *" ${line%%=*} "* ]] || printf '%s\n' "$line"
  done
  return 0
}

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# Print a blob from a local OCI layout after checking it against its digest.
# Read blobs directly: oras --oci-layout only knows the media type of manifests
# listed in index.json, and a platform manifest is listed only in its image index.
# Usage: layout_blob <layout-dir> <sha256:digest>
layout_blob() {
  local path="$1/blobs/sha256/${2#sha256:}"
  [[ "$2" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Not a sha256 digest: '$2'."
  [ -f "$path" ] || die "Blob $2 is not in the OCI layout $1."
  [ "$(sha256_of "$path")" = "${2#sha256:}" ] || die "Blob $2 in $1 does not match its digest."
  cat "$path"
}

# BuildKit attestation-manifests carry a `subject`. On registries without the
# Referrers API (e.g. GHCR), oras would otherwise create a visible
# `sha256-<digest>` fallback tag for each one; BuildKit itself never does.
ORAS_REFERRERS_SPEC="v1.1-referrers-api"

# actions/attest rejects predicates over 16 MiB (MAX_PREDICATE_SIZE_BYTES).
ATTEST_MAX_SBOM_BYTES=$((16 * 1024 * 1024))

SIGSTORE_BUNDLE_TYPE="application/vnd.dev.sigstore.bundle.v0.3+json"

# The SBOM predicate type for BuildKit's attestations and the signed bundles:
# unversioned, the only form Trivy recognises (see attest-sbom.sh).
SPDX_PREDICATE_TYPE="https://spdx.dev/Document"

# Split an image reference into REF_REPO / REF_TAG / REF_DIGEST.
# Handles registry ports (localhost:5000/img:tag) and name:tag@digest forms.
parse_ref() {
  local ref="$1" last
  REF_DIGEST=""
  REF_TAG=""
  if [[ "$ref" == *@* ]]; then
    REF_DIGEST="${ref#*@}"
    ref="${ref%%@*}"
  fi
  last="${ref##*/}"
  if [[ "$last" == *:* ]]; then
    REF_TAG="${last##*:}"
    REF_REPO="${ref%:*}"
  else
    REF_REPO="$ref"
  fi
}

# A repository is fully qualified when its first path component names a
# registry host. Short names (ubuntu, myorg/img) silently resolve to Docker Hub.
is_fully_qualified() {
  local host="${1%%/*}"
  [[ "$1" == */* ]] && { [[ "$host" == *.* ]] || [[ "$host" == *:* ]] || [[ "$host" == localhost ]]; }
}

# Fail unless a tag is a fully-qualified name:tag (no digest). Sets REF_* vars.
require_publishable_tag() {
  parse_ref "$1"
  [ -z "$REF_DIGEST" ] || die "Tag '$1' contains a digest; pass name:tag only."
  [ -n "$REF_TAG" ] || die "Tag '$1' has no tag component."
  is_fully_qualified "$REF_REPO" || die "Tag '$1' is not fully qualified with a registry host (e.g. ghcr.io/...)."
}

# Fail unless an image reference is pinned by digest (name[:tag]@sha256:<64 hex>).
# The whole reference is checked, not just its suffix: callers interpolate it
# into CSV attributes (attest=type=sbom,generator=<ref>), where a comma, quote,
# `=` or whitespace would split off the digest and leave a mutable tag behind.
# Usage: require_digest_pinned <ref> <what-it-is>
require_digest_pinned() {
  [[ "$1" =~ ^[a-z0-9]([a-z0-9._/:-]*[a-z0-9])?(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}$ ]] \
    || die "$2 must be pinned by digest (name@sha256:...), got '$1'. Tags are mutable and are not accepted."
}

# Images the actions run are pinned in .github/actions/tool-images.json and mirrored to
# the org registry by mirror-tool-images.sh. Print one as <mirror>@<digest>.
# Usage: pinned_tool_image <name>
TOOL_IMAGES_JSON="$(dirname "${BASH_SOURCE[0]}")/../../tool-images.json"
pinned_tool_image() {
  jq -er --arg n "$1" '.[$n] | "\(.mirror)@\(.digest)"' "$TOOL_IMAGES_JSON" \
    || die "No pinned image '$1' in ${TOOL_IMAGES_JSON}."
}

# Attestations need a BuildKit driver (docker-container, kubernetes, remote).
# The classic docker driver either errors or silently drops them.
# Usage: require_attesting_builder [builder-name]
require_attesting_builder() {
  local args=() driver
  [ -n "${1:-}" ] && args=(--builder "$1")
  # awk reads to EOF (no early `exit`) so the writer never gets SIGPIPE under pipefail.
  driver="$(docker buildx inspect "${args[@]}" | awk -F': *' '/^Driver:/ && !found {print $2; found = 1}')"
  log "buildx driver: ${driver:-unknown}"
  if [ "$driver" = docker ]; then
    die "The active buildx builder uses the 'docker' driver, which cannot produce provenance/SBOM attestations. Run docker/setup-buildx-action before this action."
  fi
}

# BuildKit exports cache while it builds, before the gate runs, so a cache-to
# that leaves the runner uploads the layers of an image that may then fail the
# gate. inline (cache metadata inside the image) and local stay put.
# Usage: warn_ungated_cache <what> <cache-to entry...>
warn_ungated_cache() {
  local what="$1" entry type
  shift
  for entry in "$@"; do
    [ -n "$entry" ] || continue
    type="$(printf '%s' "$entry" | tr ',' '\n' | sed -n 's/^type=//p' | head -n1)"
    case "${type:-registry}" in
      inline | local) ;;
      *) echo "::warning::${what}: cache-to '$(oneline "$entry")' uploads build cache during the build, before the Trivy and label gates run. The layers of an image that fails the gate still reach that cache. See 'Failure modes and gotchas' in .github/actions/README.md." ;;
    esac
  done
}

# Print "os/arch[/variant]" for every real platform manifest in an OCI index,
# skipping BuildKit attestation-manifests (platform unknown/unknown).
index_platforms() {
  jq -r '
    .manifests[]
    | select((.annotations["vnd.docker.reference.type"] // "") == "")
    | .platform | [.os, .architecture, .variant] | map(select(. != null and . != "")) | join("/")'
}
