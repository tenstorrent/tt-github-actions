#!/bin/bash
# Sign each platform's enriched SBOM with actions/attest and push the Sigstore
# bundle to the registry as an OCI referrer of that platform image. This is
# what `trivy image --sbom-sources oci` and `gh attestation verify` look for;
# BuildKit's unsigned attestation-manifests are invisible to both.
#
# Trivy looks up referrers of the digest it resolved: the platform digest when
# given repo@<platform-digest>, the index digest when given a tag. An index
# referrer is only added for single-platform images. Trivy uses the first SBOM
# referrer it finds, so one per platform on a multi-arch index would scan every
# platform with an arbitrary platform's SBOM.
#
# The SBOM is passed as a custom predicate, not via sbom-path: sbom-path sets
# predicateType https://spdx.dev/Document/v2.3, and Trivy only recognises the
# unversioned https://spdx.dev/Document (in-toto PredicateSPDX), which is also
# what BuildKit uses.
#
# Public repositories sign with the public-good Sigstore instance (Rekor
# entry); private/internal ones with GitHub's private instance. Requires
# `permissions: id-token: write, attestations: write` (plus `artifact-metadata:
# write` for storage records). Private repositories need GitHub Enterprise Cloud.
#
# Usage: attest-sbom.sh <repo>@<index-digest> <sboms.jsonl from enrich-sbom.sh>
# Env:   GITHUB_TOKEN, ATTEST_CREATE_STORAGE_RECORD (default true)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REF="${1:?Usage: $0 <repo>@<index-digest> <sboms.jsonl>}"
LIST="${2:?Usage: $0 <repo>@<index-digest> <sboms.jsonl>}"
parse_ref "$REF"
REPO="$REF_REPO"
[ -n "$REF_DIGEST" ] || die "attest-sbom.sh needs a digest reference, got '$REF'."

ATTEST_ROOT="${RUNNER_TEMP:-/tmp}/docker-publish-attest"
NODE="$ATTEST_ROOT/node/bin/node"
ATTEST_JS="$ATTEST_ROOT/attest/dist/index.js"
[ -x "$NODE" ] && [ -f "$ATTEST_JS" ] || die "actions/attest is not installed; run install-tools.sh with INSTALL_ATTEST=true."
[ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || die "SBOM attestation needs 'permissions: id-token: write' (and attestations: write) on the calling job."

attest() {  # <subject-digest> <sbom> <label>
  local out
  out="$(mktemp)"
  log "[$3] attesting SBOM for ${REPO}@$1"
  env \
    "INPUT_SUBJECT-NAME=${REPO}" \
    "INPUT_SUBJECT-DIGEST=$1" \
    "INPUT_PREDICATE-TYPE=${SPDX_PREDICATE_TYPE}" \
    "INPUT_PREDICATE-PATH=$2" \
    "INPUT_PUSH-TO-REGISTRY=true" \
    "INPUT_CREATE-STORAGE-RECORD=${ATTEST_CREATE_STORAGE_RECORD:-true}" \
    "INPUT_SHOW-SUMMARY=true" \
    "INPUT_GITHUB-TOKEN=${GITHUB_TOKEN:?GITHUB_TOKEN is required}" \
    GITHUB_OUTPUT="$out" \
    "$NODE" "$ATTEST_JS" < /dev/null >&2 || { rm -f "$out"; die "[$3] actions/attest failed for ${REPO}@$1"; }
  log "[$3] $(grep -E '^attestation-url' "$out" | head -1 || true)"
  rm -f "$out"
}

COUNT="$(grep -c . "$LIST" || true)"
[ "$COUNT" -gt 0 ] || die "No SBOMs listed in ${LIST}."
# Attest the index too only when it holds exactly one real platform image
# (recorded by enrich-sbom.sh from the index itself, not from the SBOM count).
INDEX_PLATFORM_COUNT="$(grep -m1 . "$LIST" | jq -r '.index_platforms // empty')"
[[ "$INDEX_PLATFORM_COUNT" =~ ^[0-9]+$ ]] || die "${LIST} does not record the index's platform count; regenerate it with enrich-sbom.sh."
# enrich-sbom.sh already fitted every SBOM to the limit. Check them all before
# signing any: a signature, once made, cannot be withdrawn.
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  sbom="$(jq -r .sbom <<< "$entry")"
  [ -f "$sbom" ] || die "SBOM ${sbom} listed in ${LIST} does not exist."
  [ "$(wc -c < "$sbom" | tr -d ' ')" -le "$ATTEST_MAX_SBOM_BYTES" ] \
    || die "SBOM ${sbom} is over ${ATTEST_MAX_SBOM_BYTES} bytes; regenerate it with enrich-sbom.sh, which fits it to the attestation limit."
done < "$LIST"

while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  digest="$(jq -r .digest <<< "$entry")"
  platform="$(jq -r .platform <<< "$entry")"
  sbom="$(jq -r .sbom <<< "$entry")"
  attest "$digest" "$sbom" "$platform"
  if [ "$INDEX_PLATFORM_COUNT" -eq 1 ]; then
    attest "$REF_DIGEST" "$sbom" "index"
  fi
done < "$LIST"
