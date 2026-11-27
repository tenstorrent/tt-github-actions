#!/bin/bash
# Validate docker-publish inputs before anything is built or pushed.
#
# Env: INPUT_TAGS (newline/comma separated), INPUT_BUILDER (optional),
#      INPUT_SBOM_GENERATOR (optional; must be pinned by digest. Empty uses
#      the org mirror pinned in .github/actions/tool-images.json),
#      INPUT_SBOM (default true), ATTEST_SBOM (default true; forced off without
#      an SBOM), INPUT_CACHE_TO (warned about if it leaves the runner),
#      INPUT_IMAGE_OUTPUT_OPTIONS (exporter options, checked against an allowlist),
#      INPUT_LABELS (key=value lines; see effective_labels in lib.sh)
# Outputs: primary-repo, report-id, sbom-generator, sbom, attest-sbom,
#          tags (normalized, newline separated), labels (for the build)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TAGS=()
while IFS= read -r t; do
  t="${t#"${t%%[![:space:]]*}"}"
  t="${t%"${t##*[![:space:]]}"}"
  [ -n "$t" ] && TAGS+=("$t")
done < <(printf '%s\n' "${INPUT_TAGS}" | tr ',' '\n')

[ "${#TAGS[@]}" -gt 0 ] || die "No tags provided. Pass fully-qualified tags, e.g. ghcr.io/tenstorrent/<image>:<tag> (docker/metadata-action's tags output works as-is)."

for t in "${TAGS[@]}"; do
  require_publishable_tag "$t"
done

parse_ref "${TAGS[0]}"
PRIMARY_REPO="$REF_REPO"

require_attesting_builder "${INPUT_BUILDER:-}"

LABELS="$(effective_labels "${INPUT_LABELS:-}")"

# image-output-options is appended to the build's own `type=oci,dest=...`
# output. A second line would add another output, and a repeated type, dest,
# tar, push or name would override ours (the last value wins), either of which
# could push before the gate. Only options that change how the layout is
# written are accepted. Fields are CSV, so a quoted field may hold commas.
OUTPUT_OPTS="${INPUT_IMAGE_OUTPUT_OPTIONS:-}"
# A YAML block scalar (`|`) ends in a newline; trailing blank lines add no output.
OUTPUT_OPTS="${OUTPUT_OPTS%"${OUTPUT_OPTS##*[!$'\n\r']}"}"
case "$OUTPUT_OPTS" in
  *$'\n'* | *$'\r'*) die "image-output-options must be one line of comma-separated exporter options." ;;
esac
OPT_FIELDS=()
field="" quoted=false
for ((i = 0; i < ${#OUTPUT_OPTS}; i++)); do
  c="${OUTPUT_OPTS:i:1}"
  if [ "$c" = '"' ]; then
    if [ "$quoted" = true ]; then quoted=false; else quoted=true; fi
  elif [ "$c" = , ] && [ "$quoted" = false ]; then
    OPT_FIELDS+=("$field")
    field=""
    continue
  fi
  field+="$c"
done
OPT_FIELDS+=("$field")
for field in "${OPT_FIELDS[@]}"; do
  key="${field#"${field%%[![:space:]]*}"}"
  key="${key#\"}"
  key="${key%%=*}"
  [ -n "$key" ] || continue
  case "$key" in
    compression | compression-level | force-compression | oci-mediatypes | oci-artifact | rewrite-timestamp \
      | annotation | annotation.* | annotation-*) ;;
    *) die "image-output-options: '${key}' is not accepted. Allowed: compression, compression-level, force-compression, oci-mediatypes, oci-artifact, rewrite-timestamp, annotation*." ;;
  esac
done

mapfile -t CACHE_TO < <(printf '%s\n' "${INPUT_CACHE_TO:-}")
warn_ungated_cache "docker-publish" "${CACHE_TO[@]}"

case "${INPUT_SBOM:-true}" in
  true) SBOM=true ;;
  false) SBOM=false ;;
  *) die "sbom must be true or false; got '${INPUT_SBOM}'" ;;
esac
ATTEST_SBOM="${ATTEST_SBOM:-true}"
if [ "$SBOM" = false ] && [ "$ATTEST_SBOM" = true ]; then
  log "sbom is false: no SBOM is generated, so none is signed (attest-sbom ignored)"
  ATTEST_SBOM=false
fi

# The SBOM scanner runs inside the build with access to the image's filesystem.
SBOM_GENERATOR="${INPUT_SBOM_GENERATOR:-}"
[ -n "$SBOM_GENERATOR" ] || SBOM_GENERATOR="$(pinned_tool_image buildkit-syft-scanner)"
require_digest_pinned "$SBOM_GENERATOR" "sbom-generator"

# Fail before building, not after the scan; attest-sbom.sh checks this again.
if [ "$ATTEST_SBOM" = true ] && [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
  die "SBOM attestation needs 'permissions: id-token: write' (and attestations: write) on the calling job."
fi

log "primary repository: $PRIMARY_REPO"
{
  echo "primary-repo=$PRIMARY_REPO"
  echo "sbom-generator=$SBOM_GENERATOR"
  echo "sbom=$SBOM"
  echo "attest-sbom=$ATTEST_SBOM"
  echo "report-id=$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
  echo "tags<<__TAGS__"
  printf '%s\n' "${TAGS[@]}"
  echo "__TAGS__"
  echo "labels<<__LABELS__"
  [ -z "$LABELS" ] || printf '%s\n' "$LABELS"
  echo "__LABELS__"
} >> "${GITHUB_OUTPUT:-/dev/stdout}"
