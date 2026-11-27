#!/bin/bash
# Copy every image pinned in tool-images.json from its upstream registry to the
# org mirror, by digest. A registry-to-registry copy keeps the digest, so the
# mirror is byte-identical to upstream and the actions keep pinning the same
# sha256; only the host they pull from changes. Upstream is contacted when a
# pin changes, not on every build.
#
# Each image also gets a `<tag>-<first 12 hex of digest>` tag in the mirror.
# Nothing pulls by that tag: it keeps registry cleanup policies, which delete
# untagged versions, from removing a digest the actions still use.
#
# Safe to re-run: an image already in the mirror at the pinned digest is left
# alone.
#
# Usage: mirror-tool-images.sh [tool-images.json]
# Requires: oras (logged in to the mirror registry with push access), jq.

set -euo pipefail
HERE="$(dirname "${BASH_SOURCE[0]}")"
source "${HERE}/docker-publish/scripts/lib.sh"

PINS="${1:-${HERE}/tool-images.json}"
jq -e 'type == "object" and length > 0' "$PINS" >/dev/null || die "${PINS} lists no images."

while IFS= read -r name; do
  [ -n "$name" ] || continue
  source="$(jq -r --arg n "$name" '.[$n].source // ""' "$PINS")"
  tag="$(jq -r --arg n "$name" '.[$n].tag // ""' "$PINS")"
  digest="$(jq -r --arg n "$name" '.[$n].digest // ""' "$PINS")"
  mirror="$(jq -r --arg n "$name" '.[$n].mirror // ""' "$PINS")"
  [ -n "$source" ] && [ -n "$tag" ] && [ -n "$mirror" ] || die "${name}: source, tag and mirror are all required in ${PINS}."
  require_digest_pinned "${source}@${digest}" "${name} in ${PINS}"

  keep="${tag}-${digest:7:12}"
  if [ "$(oras resolve "${mirror}:${keep}" 2>/dev/null || true)" = "$digest" ]; then
    log "${name}: ${mirror}@${digest} already mirrored"
    continue
  fi

  log "${name}: copying ${source}@${digest} to ${mirror}:${keep}"
  # BuildKit attestation-manifests in the image carry a subject; see lib.sh.
  oras cp --to-distribution-spec "$ORAS_REFERRERS_SPEC" "${source}@${digest}" "${mirror}:${keep}" >&2
  got="$(oras resolve "${mirror}:${keep}")"
  [ "$got" = "$digest" ] || die "${name}: ${mirror}:${keep} resolved to ${got}, expected ${digest}."
  log "${name}: mirrored ${mirror}@${digest}"
done < <(jq -r 'keys[]' "$PINS")
