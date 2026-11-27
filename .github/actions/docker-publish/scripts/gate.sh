#!/bin/bash
# Gate every built image while it is still a local OCI layout, then push them by
# digest and enrich, attest and copy them. Nothing reaches a registry until every
# image has passed the gates, and every image is pushed, enriched, attested and
# copied before any tag is applied. The final tag phase is a sequence of
# registry tag operations that registries cannot make atomic, so a registry
# failure in that phase can still leave some tags applied.
#
# Usage: gate.sh <images.json>
#   images.json: [{"name": "...", "repo": "...", "digest": "sha256:...",
#                  "layout": "/path/to/oci-layout", "tags": ["reg/repo:tag", ...]}]
#   The layouts are deleted when gate.sh exits, pass or fail.
# Env:   INPUT_REQUIRED_LABELS, INPUT_SEVERITY, INPUT_SCANNERS, INPUT_IGNORE_UNFIXED,
#        INPUT_IGNOREFILE, INPUT_FAIL_ON_VULNS, INPUT_ALLOWED_PRIVATE_KEYS,
#        INPUT_HARBOR_PREFIX, SBOM_SUPPLIER, REPORT_ROOT,
#        SBOM (default true; false publishes each image as built, with no SBOM),
#        ATTEST_SBOM (default true), GITHUB_TOKEN, ATTEST_CREATE_STORAGE_RECORD
# Outputs: images (JSON, digests after enrichment), digest (first image)

set -euo pipefail
SCRIPTS="$(dirname "${BASH_SOURCE[0]}")"
source "${SCRIPTS}/lib.sh"

IMAGES_JSON="${1:?Usage: $0 <images.json>}"
REPORT_ROOT="${REPORT_ROOT:?}"
COUNT="$(jq length "$IMAGES_JSON")"
[ "$COUNT" -gt 0 ] || die "No built images to gate."

field() { jq -r --argjson i "$1" ".[\$i].$2" "$IMAGES_JSON"; }

SBOM_ROOT="$(mktemp -d)"
mapfile -t LAYOUTS < <(jq -r '.[].layout // empty' "$IMAGES_JSON")
[ "${#LAYOUTS[@]}" -eq "$COUNT" ] || die "Every image in ${IMAGES_JSON} needs a 'layout'."
trap 'rm -rf "$SBOM_ROOT" "${LAYOUTS[@]}"' EXIT

# Without an SBOM there is nothing to sign (prepare.sh already said so).
[ "${SBOM:-true}" = true ] || ATTEST_SBOM=false

FAILED=()
for ((i = 0; i < COUNT; i++)); do
  name="$(field "$i" name)"
  ref="$(field "$i" layout)@$(field "$i" digest)"
  echo "::group::[${name}] verify OCI labels"
  bash "${SCRIPTS}/verify-labels.sh" "$ref" || FAILED+=("${name}: labels")
  echo "::endgroup::"
  echo "::group::[${name}] Trivy scan"
  IMAGE_LABEL="${name} $(field "$i" digest)" REPORT_DIR="${REPORT_ROOT}/${name//[^A-Za-z0-9._-]/_}" \
    bash "${SCRIPTS}/trivy-scan.sh" "$ref" || FAILED+=("${name}: trivy")
  echo "::endgroup::"
done

if [ "${#FAILED[@]}" -gt 0 ]; then
  printf '::error::Gate failed: %s\n' "${FAILED[@]}"
  die "Nothing was pushed to any registry."
fi

# Push every image by digest, with no tag. The layout is deleted as soon as its
# image is pushed: syft reads the image back from the registry.
for ((i = 0; i < COUNT; i++)); do
  name="$(field "$i" name)"
  repo="$(field "$i" repo)"
  digest="$(field "$i" digest)"
  layout="$(field "$i" layout)"
  echo "::group::[${name}] push by digest"
  # No destination tag: oras copies the graph and creates no tag. GHCR can
  # answer "not found" for a manifest it has just accepted (the index was in
  # the registry moments after oras reported it missing), so the copy and the
  # check are retried. A retry re-sends only what the registry lacks.
  pushed=""
  for attempt in 1 2 3 4; do
    if oras cp --from-oci-layout --to-distribution-spec "$ORAS_REFERRERS_SPEC" "${layout}@${digest}" "$repo" >&2; then
      pushed="$(oras manifest fetch --descriptor "${repo}@${digest}" | jq -r .digest)" || pushed=""
      [ "$pushed" = "$digest" ] && break
    fi
    [ "$attempt" -lt 4 ] && { log "push of ${repo}@${digest} not confirmed (attempt ${attempt}/4); retrying in $((attempt * 10))s"; sleep $((attempt * 10)); }
  done
  [ "$pushed" = "$digest" ] || die "${repo}@${digest} is not in the registry after 4 push attempts."
  log "pushed ${repo}@${digest}"
  rm -rf "$layout"
  echo "::endgroup::"
done

# Enrich and attest every image before tagging any, so a failure on a later
# image cannot leave earlier ones tagged.
#
# Every image is enriched before any is signed: enrich-sbom.sh fails if an SBOM
# cannot be fitted to the attestation size limit, and a signature, once made,
# cannot be withdrawn.
DIGESTS=()
for ((i = 0; i < COUNT; i++)); do
  name="$(field "$i" name)"
  repo="$(field "$i" repo)"
  if [ "${SBOM:-true}" = false ]; then
    # No SBOM to enrich: the image is published as built.
    DIGESTS+=("$(field "$i" digest)")
    continue
  fi
  echo "::group::[${name}] enrich SBOM"
  if [ "${ATTEST_SBOM:-true}" = true ]; then
    mkdir "${SBOM_ROOT}/${i}"
    digest="$(SBOM_OUT_DIR="${SBOM_ROOT}/${i}" bash "${SCRIPTS}/enrich-sbom.sh" "${repo}@$(field "$i" digest)")"
  else
    digest="$(SBOM_OUT_DIR="" bash "${SCRIPTS}/enrich-sbom.sh" "${repo}@$(field "$i" digest)")"
  fi
  DIGESTS+=("$digest")
  echo "::endgroup::"
done

if [ "${ATTEST_SBOM:-true}" = true ]; then
  for ((i = 0; i < COUNT; i++)); do
    name="$(field "$i" name)"
    echo "::group::[${name}] sign SBOM"
    bash "${SCRIPTS}/attest-sbom.sh" "$(field "$i" repo)@${DIGESTS[$i]}" "${SBOM_ROOT}/${i}/sboms.jsonl"
    echo "::endgroup::"
  done
fi

# Stage every image first (copies with no tags), so a failed copy leaves no tag behind.
for ((i = 0; i < COUNT; i++)); do
  name="$(field "$i" name)"
  repo="$(field "$i" repo)"
  digest="${DIGESTS[$i]}"
  echo "::group::[${name}] copy to other repositories"
  INPUT_TAGS="$(jq -r --argjson i "$i" '.[$i].tags[]' "$IMAGES_JSON")" \
    PUBLISH_PHASE=stage bash "${SCRIPTS}/publish-tags.sh" "${repo}@${digest}"
  echo "::endgroup::"
done

echo "### Published" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
RESULT='[]'
for ((i = 0; i < COUNT; i++)); do
  name="$(field "$i" name)"
  repo="$(field "$i" repo)"
  digest="${DIGESTS[$i]}"
  echo "::group::[${name}] publish tags"
  INPUT_TAGS="$(jq -r --argjson i "$i" '.[$i].tags[]' "$IMAGES_JSON")" \
    PUBLISH_PHASE=tag bash "${SCRIPTS}/publish-tags.sh" "${repo}@${digest}"
  echo "::endgroup::"
  RESULT="$(jq -c --argjson i "$i" --arg d "$digest" --argjson r "$RESULT" \
    '$r + [.[$i] | {name, repo, digest: $d, build_digest: .digest, tags}]' "$IMAGES_JSON")"
done

{
  echo "images=${RESULT}"
  echo "digest=$(printf '%s' "$RESULT" | jq -r '.[0].digest')"
} >> "${GITHUB_OUTPUT:-/dev/stdout}"
