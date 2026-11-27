#!/bin/bash
# Warn when a platform image lacks one of the OCI labels the SBOM enrichment
# and traceability depend on. Missing labels never fail the gate: the image is
# published, and the SBOM fields they would fill stay NOASSERTION. Only a
# malformed layout (no image manifests, a blob that fails its digest) fails.
#
# Usage: verify-labels.sh <oci-layout-dir>@<index-digest>
# Env:   INPUT_REQUIRED_LABELS (whitespace/comma separated label keys)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REF="${1:?Usage: $0 <oci-layout-dir>@<index-digest>}"
LAYOUT="${REF%@*}"

read -r -a REQUIRED <<< "$(printf '%s' "${INPUT_REQUIRED_LABELS:-}" | tr ',\n' '  ')"
[ "${#REQUIRED[@]}" -gt 0 ] || { log "No required labels configured."; exit 0; }

INDEX_JSON="$(layout_blob "$LAYOUT" "${REF##*@}")"
mapfile -t IMAGE_DIGESTS < <(printf '%s' "$INDEX_JSON" | jq -r '
  .manifests[] | select((.annotations["vnd.docker.reference.type"] // "") == "") | .digest')
[ "${#IMAGE_DIGESTS[@]}" -gt 0 ] || die "No image manifests found in $REF."

missing=0
for d in "${IMAGE_DIGESTS[@]}"; do
  config="$(layout_blob "$LAYOUT" "$d" | jq -r .config.digest)"
  labels="$(layout_blob "$LAYOUT" "$config" | jq -c '.config.Labels // {}')"
  for key in "${REQUIRED[@]}"; do
    value="$(printf '%s' "$labels" | jq -r --arg k "$key" '.[$k] // ""')"
    if [ -z "$value" ]; then
      echo "::warning title=Missing OCI label::Image manifest ${d} is missing expected label '${key}'."
      missing=1
    else
      log "${d}: ${key}=$(oneline "$value")"
    fi
  done
done

if [ "$missing" -ne 0 ]; then
  echo "::warning title=Missing OCI labels::Expected OCI labels are missing; the image is published anyway. Set them via the 'labels' input (docker/metadata-action's labels output provides source/revision/licenses) or LABEL in the Dockerfile."
fi
