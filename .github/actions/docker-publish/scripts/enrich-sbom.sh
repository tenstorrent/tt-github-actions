#!/bin/bash
# Replace BuildKit's directory-scanned SBOM attestations with ones enriched from
# each image's own OCI labels (org.opencontainers.image.source/licenses/version).
#
# WHY: BuildKit generates its SBOM attestation by mounting the exported image's
# rootfs as a plain DIRECTORY to the scanner (see moby/buildkit
# docs/attestations/sbom-protocol.md) - the scanner never receives the image
# Config/Labels, so the SBOM's describing package always has
# licenseDeclared/downloadLocation/supplier = NOASSERTION even when the correct
# values are present in the image's config Labels one manifest over. syft cannot
# auto-map OCI labels into SPDX fields either (anchore/syft#3098). This script
# rescans each platform image from the registry with syft, jq-patches the
# describing package, and rebuilds the attestation-manifests and the index,
# preserving BuildKit's OCI shape (see tenstorrent/tt-metal#49882).
#
# It also adds what Trivy's SPDX decoder needs to scan the SBOM as accurately
# as the image itself, plus per-package suppliers and layers (enrich-sbom.jq).
#
# Every platform in the index is processed. Non-SBOM attestation layers (SLSA
# provenance etc.) and the in-toto subject are carried over byte-for-byte.
#
# The org.opencontainers.image.* labels that every platform image shares are
# also copied onto the index as annotations. A tag resolves to the index, and
# GitHub's package page reads only the index's annotations, never the platform
# configs, so without them description, source and license show as blank.
# Annotations already on the index win.
#
# The enriched index is pushed BY DIGEST only; tagging is left to the caller so
# nothing becomes pullable by tag until all gates have passed.
#
# Usage: enrich-sbom.sh <repo>@<index-digest>
#   Prints the new index digest on stdout; logs go to stderr.
# Env:   SBOM_SUPPLIER (default "Organization: Tenstorrent")
#        SBOM_OUT_DIR  set it when the SBOMs will be signed (attest-sbom.sh).
#                      Each platform's SBOM is written there in a form
#                      actions/attest accepts (at most 16 MiB): the full SBOM,
#                      or one without file-level entries whose comment names
#                      the registry blob holding the full one. Fails if even
#                      that is too large. Listed in sboms.jsonl ({digest,
#                      platform, sbom, index_platforms: real platform images
#                      in the index, full_sbom_statement, file_entries})
# Requires: oras, syft, jq. Registry auth comes from the docker credential store.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REF="${1:?Usage: $0 <repo>@<index-digest>}"
SUPPLIER="${SBOM_SUPPLIER:-Organization: Tenstorrent}"
SPDX_PREDICATE="$SPDX_PREDICATE_TYPE"

parse_ref "$REF"
REPO="$REF_REPO"
[ -n "$REF_DIGEST" ] || die "enrich-sbom.sh needs a digest reference (<repo>@sha256:...), got '$REF'."

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

INDEX_JSON="$(oras manifest fetch "${REPO}@${REF_DIGEST}")"
if ! printf '%s' "$INDEX_JSON" | jq -e '.manifests' >/dev/null 2>&1; then
  die "${REF} is not an OCI index; it carries no attestations to enrich."
fi
INDEX_MT="$(printf '%s' "$INDEX_JSON" | jq -r '.mediaType // "application/vnd.oci.image.index.v1+json"')"
INDEX_PLATFORMS="$(printf '%s' "$INDEX_JSON" | index_platforms)"
INDEX_PLATFORM_COUNT="$(printf '%s' "$INDEX_PLATFORMS" | grep -c . || true)"

mapfile -t ATT_DIGESTS < <(printf '%s' "$INDEX_JSON" | jq -r '
  .manifests[] | select(.annotations["vnd.docker.reference.type"] == "attestation-manifest") | .digest')
[ "${#ATT_DIGESTS[@]}" -gt 0 ] || die "No attestation-manifests in ${REF}; was the image built with SBOM attestations enabled?"
# BuildKit writes one attestation-manifest per platform image; fewer means a
# platform would be published with no SBOM at all.
[ "${#ATT_DIGESTS[@]}" -eq "$INDEX_PLATFORM_COUNT" ] \
  || die "${REF} has ${INDEX_PLATFORM_COUNT} platform image(s) but ${#ATT_DIGESTS[@]} attestation-manifest(s); every published platform image needs an SBOM."

NEW_INDEX="$WORKDIR/index.json"
printf '%s' "$INDEX_JSON" > "$NEW_INDEX"

push_blob() {  # <file> <media-type> -> prints digest
  local digest
  digest="sha256:$(sha256_of "$1")"
  oras blob push "${REPO}@${digest}" --media-type "$2" "$1" >/dev/null
  echo "$digest"
}

label() { printf '%s' "$LABELS_JSON" | jq -r --arg k "org.opencontainers.image.$1" '.[$k] // ""'; }

n=0
for ATT_DIGEST in "${ATT_DIGESTS[@]}"; do
  n=$((n + 1))
  W="$WORKDIR/$n"
  mkdir -p "$W"

  IMG_DIGEST="$(printf '%s' "$INDEX_JSON" | jq -r --arg d "$ATT_DIGEST" '
    .manifests[] | select(.digest == $d) | .annotations["vnd.docker.reference.digest"]')"
  PLATFORM="$(printf '%s' "$INDEX_JSON" | jq -r --arg d "$IMG_DIGEST" '
    .manifests[] | select(.digest == $d) | .platform | [.os, .architecture, .variant] | map(select(. != null and . != "")) | join("/")')"
  log "[$PLATFORM] image ${IMG_DIGEST}, attestation ${ATT_DIGEST}"

  ATT_MANIFEST="$(oras manifest fetch "${REPO}@${ATT_DIGEST}")"
  SPDX_IDX="$(printf '%s' "$ATT_MANIFEST" | jq --arg p "$SPDX_PREDICATE" '
    [.layers | to_entries[] | select(.value.annotations["in-toto.io/predicate-type"] == $p) | .key] | .[0] // -1')"
  if [ "$SPDX_IDX" -lt 0 ]; then
    die "[$PLATFORM] attestation-manifest ${ATT_DIGEST} has no SPDX SBOM layer; an SBOM is mandatory for every published platform image."
  fi
  if [ "$(printf '%s' "$ATT_MANIFEST" | jq --arg p "$SPDX_PREDICATE" '[.layers[] | select(.annotations["in-toto.io/predicate-type"] == $p)] | length')" -gt 1 ]; then
    log "[$PLATFORM] multiple SPDX layers found; enriching the first (final-stage) one only."
  fi
  OLD_SPDX_DIGEST="$(printf '%s' "$ATT_MANIFEST" | jq -r --argjson i "$SPDX_IDX" '.layers[$i].digest')"

  # --- labels from the platform image config ---
  LABELS_JSON="$(oras manifest fetch-config "${REPO}@${IMG_DIGEST}" | jq -c '.config.Labels // {}')"
  printf '%s\n' "$LABELS_JSON" >> "$WORKDIR/labels.jsonl"
  LABEL_SOURCE="$(label source)"
  LABEL_LICENSES="$(label licenses)"
  LABEL_VERSION="$(label version)"
  log "[$PLATFORM] labels: source='$(oneline "$LABEL_SOURCE")' licenses='$(oneline "$LABEL_LICENSES")' version='$(oneline "$LABEL_VERSION")'"

  # --- rescan with syft ---
  # file.metadata.selection defaults to "owned-by-package": syft only computes
  # real file digests for files attributable to a detected package. Images of
  # raw binaries COPYed onto `FROM scratch` have no package metadata, so every
  # file is "unowned" and gets a placeholder all-zero SHA1 (anchore/syft#2307,
  # #1226). Force "all" so every file gets a real digest.
  # license.content=unknown embeds the text of licenses syft could not map to
  # an SPDX ID, so the LicenseRef-* entries can be reviewed.
  # golang.main-module-version.from-contents: when a Go binary's main module is
  # "(devel)" and -ldflags has no version, guess it from semver-like strings in
  # the binary. The guess takes precedence over the vcs pseudo-version.
  # syft-json carries what SPDX drops (distro, source packages, maintainers,
  # per-file layers, raw manifest/config) and feeds the Trivy-compat patch.
  SYFT_RAW_SPDX="$W/syft.raw.spdx.json"
  SYFT_JSON="$W/syft.json"
  SYFT_ARGS=(scan "registry:${REPO}@${IMG_DIGEST}" --source-name "$REPO"
    -o "spdx-json=${SYFT_RAW_SPDX}" -o "syft-json=${SYFT_JSON}")
  [ -n "$LABEL_VERSION" ] && SYFT_ARGS+=(--source-version "$LABEL_VERSION")
  SYFT_FILE_METADATA_SELECTION=all SYFT_LICENSE_CONTENT=unknown \
    SYFT_GOLANG_MAIN_MODULE_VERSION_FROM_CONTENTS=true syft "${SYFT_ARGS[@]}" >&2

  # --- OS package, source packages, suppliers, layers (see enrich-sbom.jq) ---
  SYFT_SPDX="$W/syft.spdx.json"
  jq --slurpfile syft "$SYFT_JSON" --arg annotator "Tool: tenstorrent-docker-publish" \
    -f "$(dirname "${BASH_SOURCE[0]}")/enrich-sbom.jq" "$SYFT_RAW_SPDX" > "$SYFT_SPDX"
  log "[$PLATFORM] $(jq -r '
    ([.packages[] | select(.SPDXID | startswith("SPDXRef-OperatingSystem"))][0] | if . then "os=\(.name) \(.versionInfo)" else "os=none" end)
    + ", source-packages=\([.packages[] | select(.sourceInfo // "" | startswith("built package from"))] | length)"
    + ", with-supplier=\([.packages[] | select((.supplier // "NOASSERTION") != "NOASSERTION")] | length)"
    + ", with-layer=\([.packages[] | select(any(.annotations[]?; .comment | startswith("LayerDiffID: ")))] | length)"
    + " of \(.packages | length) packages"' "$SYFT_SPDX")"

  # --- patch the describing/root package from the OCI labels ---
  ROOT_ID="$(jq -r '
    (.documentDescribes[0])
    // ([.relationships[]? | select(.spdxElementId == "SPDXRef-DOCUMENT" and .relationshipType == "DESCRIBES")][0].relatedSpdxElement)
    // ([.packages[] | select(.SPDXID | test("DocumentRoot"))][0].SPDXID)
    // ""' "$SYFT_SPDX")"
  PATCHED_SPDX="$W/spdx.patched.json"
  if [ -z "$ROOT_ID" ]; then
    log "[$PLATFORM] no describing/root package in the syft SBOM; leaving packages unpatched."
    cp "$SYFT_SPDX" "$PATCHED_SPDX"
  else
    jq --arg root "$ROOT_ID" --arg lic "$LABEL_LICENSES" --arg src "$LABEL_SOURCE" --arg sup "$SUPPLIER" '
      .packages |= map(
        if .SPDXID == $root then
          (if $lic != "" then .licenseDeclared = $lic else . end)
          | (if $src != "" then .downloadLocation = $src else . end)
          | .supplier = $sup
        else . end)
    ' "$SYFT_SPDX" > "$PATCHED_SPDX"
  fi

  # --- new in-toto statement: BuildKit's original envelope, new predicate ---
  OLD_STATEMENT="$W/spdx.orig.intoto.json"
  oras blob fetch "${REPO}@${OLD_SPDX_DIGEST}" --output "$OLD_STATEMENT"
  SPDX_LAYER="$W/spdx.intoto.json"
  jq -c --slurpfile spdx "$PATCHED_SPDX" '.predicate = $spdx[0]' "$OLD_STATEMENT" > "$SPDX_LAYER"
  NEW_SPDX_DIGEST="$(push_blob "$SPDX_LAYER" application/vnd.in-toto+json)"
  NEW_SPDX_SIZE="$(wc -c < "$SPDX_LAYER" | tr -d ' ')"

  # --- SBOM to sign: the full one if actions/attest accepts it, else without
  # file-level entries and bound to the full one by the digest of the statement
  # blob just pushed. Checked here, before this platform's attestation-manifest
  # and the index are pushed and before anything is signed. ---
  if [ -n "${SBOM_OUT_DIR:-}" ]; then
    SIGNABLE="${SBOM_OUT_DIR}/${IMG_DIGEST#sha256:}.spdx.json"
    # Compact JSON: indentation would count against the limit.
    jq -c . "$PATCHED_SPDX" > "$SIGNABLE"
    FULL_BYTES="$(wc -c < "$SIGNABLE" | tr -d ' ')"
    FILE_ENTRIES=true
    if [ "$FULL_BYTES" -gt "$ATTEST_MAX_SBOM_BYTES" ]; then
      FILE_ENTRIES=false
      # File entries are what grow with image size; Trivy only uses packages,
      # relationships and annotations.
      jq -c --arg full "${REPO}@${NEW_SPDX_DIGEST}" '
        ([.files[]?.SPDXID] | map({key: ., value: true}) | from_entries) as $f
        | del(.files)
        | .packages |= map(del(.hasFiles))
        | .relationships |= map(select(($f[.spdxElementId] or $f[.relatedSpdxElement]) | not))
        | .comment = ((.comment // "" | if . == "" then "" else . + " " end)
            + "File-level entries omitted to fit the 16 MiB attestation limit. The complete SBOM is the predicate of the in-toto statement stored as blob \($full).")
      ' "$PATCHED_SPDX" > "$SIGNABLE"
      SIGNABLE_BYTES="$(wc -c < "$SIGNABLE" | tr -d ' ')"
      [ "$SIGNABLE_BYTES" -le "$ATTEST_MAX_SBOM_BYTES" ] \
        || die "[$PLATFORM] SBOM is ${FULL_BYTES} bytes, and still ${SIGNABLE_BYTES} bytes without file-level entries; actions/attest accepts at most ${ATTEST_MAX_SBOM_BYTES}. This image has not been signed or tagged."
      log "[$PLATFORM] SBOM is ${FULL_BYTES} bytes (limit ${ATTEST_MAX_SBOM_BYTES}); signing it without file-level entries (${SIGNABLE_BYTES} bytes), bound to the full SBOM in ${NEW_SPDX_DIGEST}."
    fi
    jq -cn --arg digest "$IMG_DIGEST" --arg platform "$PLATFORM" --arg sbom "$SIGNABLE" \
      --argjson index_platforms "$INDEX_PLATFORM_COUNT" \
      --arg full_sbom_statement "$NEW_SPDX_DIGEST" --argjson file_entries "$FILE_ENTRIES" \
      '{digest: $digest, platform: $platform, sbom: $sbom, index_platforms: $index_platforms,
        full_sbom_statement: $full_sbom_statement, file_entries: $file_entries}' >> "${SBOM_OUT_DIR}/sboms.jsonl"
  fi

  # --- config blob: reused as-is unless it lists diff_ids (current BuildKit
  # writes "{}"; older attestation configs carried rootfs.diff_ids) ---
  NEW_CONFIG_DIGEST="$(printf '%s' "$ATT_MANIFEST" | jq -r '.config.digest')"
  NEW_CONFIG_SIZE="$(printf '%s' "$ATT_MANIFEST" | jq -r '.config.size')"
  CONFIG_BLOB="$W/config.json"
  oras manifest fetch-config "${REPO}@${ATT_DIGEST}" > "$CONFIG_BLOB"
  if jq -e '(.rootfs.diff_ids | type) == "array"' "$CONFIG_BLOB" >/dev/null; then
    jq -c --argjson i "$SPDX_IDX" --arg d "$NEW_SPDX_DIGEST" '.rootfs.diff_ids[$i] = $d' "$CONFIG_BLOB" > "$CONFIG_BLOB.new"
    NEW_CONFIG_DIGEST="$(push_blob "$CONFIG_BLOB.new" "$(printf '%s' "$ATT_MANIFEST" | jq -r '.config.mediaType')")"
    NEW_CONFIG_SIZE="$(wc -c < "$CONFIG_BLOB.new" | tr -d ' ')"
  fi

  # --- new attestation-manifest: every other layer untouched ---
  NEW_ATT="$W/attestation-manifest.json"
  printf '%s' "$ATT_MANIFEST" | jq -c \
    --argjson i "$SPDX_IDX" \
    --arg sd "$NEW_SPDX_DIGEST" --argjson ss "$NEW_SPDX_SIZE" \
    --arg cd "$NEW_CONFIG_DIGEST" --argjson cs "$NEW_CONFIG_SIZE" '
      .layers[$i].digest = $sd | .layers[$i].size = $ss
      | .config.digest = $cd | .config.size = $cs' > "$NEW_ATT"
  ATT_MT="$(printf '%s' "$ATT_MANIFEST" | jq -r '.mediaType // "application/vnd.oci.image.manifest.v1+json"')"
  NEW_ATT_DIGEST="sha256:$(sha256_of "$NEW_ATT")"
  oras manifest push --distribution-spec "$ORAS_REFERRERS_SPEC" \
    "${REPO}@${NEW_ATT_DIGEST}" --media-type "$ATT_MT" "$NEW_ATT" >/dev/null
  log "[$PLATFORM] pushed enriched attestation-manifest ${NEW_ATT_DIGEST}"

  # --- point the index's descriptor at it, preserving its annotations ---
  jq -c --arg old "$ATT_DIGEST" --arg new "$NEW_ATT_DIGEST" --argjson size "$(wc -c < "$NEW_ATT" | tr -d ' ')" '
    .manifests |= map(if .digest == $old then .digest = $new | .size = $size else . end)
  ' "$NEW_INDEX" > "$NEW_INDEX.tmp" && mv "$NEW_INDEX.tmp" "$NEW_INDEX"
done

# --- index annotations: the OCI labels every platform agrees on (see header) ---
jq -c --slurpfile labels "$WORKDIR/labels.jsonl" -f "$(dirname "${BASH_SOURCE[0]}")/index-annotations.jq" \
  "$NEW_INDEX" > "$NEW_INDEX.tmp" && mv "$NEW_INDEX.tmp" "$NEW_INDEX"
log "index annotations: $(jq -c '.annotations // {} | keys' "$NEW_INDEX")"

NEW_INDEX_DIGEST="sha256:$(sha256_of "$NEW_INDEX")"
oras manifest push --distribution-spec "$ORAS_REFERRERS_SPEC" \
  "${REPO}@${NEW_INDEX_DIGEST}" --media-type "$INDEX_MT" "$NEW_INDEX" >/dev/null
log "pushed enriched index ${NEW_INDEX_DIGEST} (supplier='${SUPPLIER}')"
echo "$NEW_INDEX_DIGEST"
