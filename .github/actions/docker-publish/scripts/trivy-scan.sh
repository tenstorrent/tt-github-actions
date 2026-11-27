#!/bin/bash
# Scan every platform of a built image, still in a local OCI layout and not yet
# pushed anywhere, with Trivy. Secret, misconfiguration and license findings at
# the severity threshold fail the gate. Vulnerabilities at the threshold are
# reported (log, step summary, warning annotation, JSON report) and fail the
# gate only when INPUT_FAIL_ON_VULNS is true. A Trivy error always fails.
# The crypto scanner, when in INPUT_SCANNERS, runs as a separate scan that
# writes a CycloneDX CBOM next to the JSON reports: Trivy accepts it only with
# --format cyclonedx, and its findings are an inventory with no severity. Any
# private key in it fails the gate, whatever the severity threshold, unless
# INPUT_ALLOWED_PRIVATE_KEYS allows it; .trivyignore does not apply to crypto
# findings. Private keys are looked for in every layer, including files a later
# layer deletes or replaces (see layer_private_keys). Keys below 112 bits of
# classical security only get a warning.
# See https://trivy.dev/docs/latest/guide/
#
# Usage: trivy-scan.sh <oci-layout-dir>@<index-digest>
# Env:   INPUT_SEVERITY, INPUT_SCANNERS, INPUT_IGNORE_UNFIXED, INPUT_IGNOREFILE,
#        INPUT_FAIL_ON_VULNS (default false),
#        INPUT_ALLOWED_PRIVATE_KEYS (reviewed private keys; see key_allowed),
#        INPUT_HARBOR_PREFIX (pull-through prefix for Trivy's databases, e.g.
#        'harbor.example.com/'; empty pulls them from upstream),
#        REPORT_DIR, IMAGE_LABEL (heading for the step summary; default the digest)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REF="${1:?Usage: $0 <oci-layout-dir>@<index-digest>}"
LAYOUT="${REF%@*}"
REPORT_DIR="${REPORT_DIR:?}"
mkdir -p "$REPORT_DIR"
VIEWS="$(mktemp -d)"
trap 'rm -rf "$VIEWS"' EXIT

SEVERITY="${INPUT_SEVERITY:-CRITICAL,HIGH}"
SCANNERS="${INPUT_SCANNERS:-vuln,misconfig,secret,crypto}"
FAIL_ON_VULNS="${INPUT_FAIL_ON_VULNS:-false}"
HARBOR="${INPUT_HARBOR_PREFIX:-}"
scanner_on() { [[ ",${SCANNERS}," == *",$1,"* ]]; }
# Everything but crypto goes into the gating JSON scan.
GATE_SCANNERS="$(printf '%s' "$SCANNERS" | tr ',' '\n' | { grep -vx 'crypto' || true; } | paste -sd, -)"

# --exit-code 0: Trivy then exits non-zero only on an error, and which findings
# fail the gate is decided from the JSON report below. It is passed explicitly,
# not left at its default, because a caller's TRIVY_EXIT_CODE or trivy.yaml
# `exit-code` would otherwise turn findings into a scan error.
TRIVY_ARGS=(
  --exit-code 0
  --severity "$SEVERITY"
  --scanners "$GATE_SCANNERS"
  --format json
)

# --- databases: through the Harbor pull-through cache when given ---
# Harbor answers only on-prem runners, and the prefix comes from a health check
# that may have run on another runner, so it is dropped unless Harbor responds
# here. Any HTTP status (an anonymous /v2/ gets 401) counts as reachable.
if [ -n "$HARBOR" ] && ! curl -s --max-time 10 -o /dev/null "https://${HARBOR%%/*}/v2/"; then
  echo "::warning::Harbor ('${HARBOR}') is not reachable from this runner; Trivy downloads its databases from upstream."
  HARBOR=""
fi
# Trivy tries --db-repository entries in order but moves to the next only on a
# 429 or 5xx; an unreachable repository is fatal (checked against 0.76.2). So
# the vulnerability DB is downloaded here first, from Harbor and then from
# Trivy's default upstream repositories, and the scan reuses it with
# --skip-db-update. --download-db-only is a no-op while the cached DB is still
# current. The Java DB and checks bundle are fetched lazily during the scan;
# they are pointed at Harbor only once the DB download proved it serves them.
# The Java DB list keeps Trivy's defaults behind Harbor, but Trivy does not
# reach them when Harbor fails with anything but a 429 or 5xx, so a scan that
# errors with Harbor's Java DB is run once more with Trivy's defaults only
# (JAVA_DB_HARBOR_ARGS). A failed checks bundle pull falls back to the checks
# embedded in the trivy binary.
JAVA_DB_HARBOR_ARGS=()
if scanner_on vuln; then
  if [ -n "$HARBOR" ] && trivy image --download-db-only --no-progress \
       --db-repository "${HARBOR}ghcr.io/aquasecurity/trivy-db:2"; then
    JAVA_DB_HARBOR_ARGS=(
      --java-db-repository "${HARBOR}ghcr.io/aquasecurity/trivy-java-db:1"
      --java-db-repository mirror.gcr.io/aquasec/trivy-java-db:1
      --java-db-repository ghcr.io/aquasecurity/trivy-java-db:1
    )
    scanner_on misconfig && TRIVY_ARGS+=(--checks-bundle-repository "${HARBOR}ghcr.io/aquasecurity/trivy-checks:2")
  else
    [ -n "$HARBOR" ] && echo "::warning::Could not download Trivy's vulnerability DB through '${HARBOR}'; downloading it from upstream."
    trivy image --download-db-only --no-progress || die "Could not download Trivy's vulnerability DB."
  fi
  TRIVY_ARGS+=(--skip-db-update)
elif [ -n "$HARBOR" ] && scanner_on misconfig; then
  TRIVY_ARGS+=(--checks-bundle-repository "${HARBOR}ghcr.io/aquasecurity/trivy-checks:2")
fi
[ "${INPUT_IGNORE_UNFIXED:-false}" = true ] && TRIVY_ARGS+=(--ignore-unfixed)
if [ -n "${INPUT_IGNOREFILE:-}" ]; then
  [ -f "$INPUT_IGNOREFILE" ] || die "trivy-ignorefile '${INPUT_IGNOREFILE}' does not exist."
  TRIVY_ARGS+=(--ignorefile "$INPUT_IGNOREFILE")
fi

# `trivy image --input <layout>` only looks at the top-level entries of
# index.json and, for a nested index, scans its first child; --platform is not
# applied. BuildKit's layout has a single top-level entry (the index), so each
# platform gets a view whose index.json lists only that platform's descriptor
# and whose blobs/ is the build layout's.
INDEX_JSON="$(layout_blob "$LAYOUT" "${REF##*@}")"
mapfile -t PLATFORMS < <(printf '%s' "$INDEX_JSON" | index_platforms)
[ "${#PLATFORMS[@]}" -gt 0 ] || die "No platform manifests found in $REF."
mapfile -t DESCRIPTORS < <(printf '%s' "$INDEX_JSON" | jq -c '
  .manifests[] | select((.annotations["vnd.docker.reference.type"] // "") == "")')
[ "${#DESCRIPTORS[@]}" -eq "${#PLATFORMS[@]}" ] || die "Could not pair platforms with manifests in $REF."
for ((p = 0; p < ${#PLATFORMS[@]}; p++)); do
  view="${VIEWS}/${p}"
  mkdir "$view"
  ln -s "$(cd "$LAYOUT" && pwd)/blobs" "${view}/blobs"
  cp "${LAYOUT}/oci-layout" "${view}/oci-layout"
  jq -n --argjson d "${DESCRIPTORS[$p]}" \
    '{schemaVersion: 2, mediaType: "application/vnd.oci.image.index.v1+json", manifests: [$d]}' > "${view}/index.json"
done

# Private keys the caller has reviewed and accepts (INPUT_ALLOWED_PRIVATE_KEYS):
# one entry per line, blank lines and #-comments ignored. An entry is either a
# key's CBOM fingerprint, written sha256:<hex>, which allows that key at any
# path, or a glob matched against the file's path in the image (no leading
# slash; * also matches /).
ALLOWED_KEYS=()
while IFS= read -r entry; do
  entry="${entry%%#*}"
  entry="$(printf '%s' "$entry" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -n "$entry" ] && ALLOWED_KEYS+=("$entry")
done <<< "${INPUT_ALLOWED_PRIVATE_KEYS:-}"

key_allowed() {  # <fingerprint> <path>
  local entry
  for entry in "${ALLOWED_KEYS[@]}"; do
    [ "$entry" = "sha256:$1" ] && return 0
    # shellcheck disable=SC2053 # the entry is a glob on purpose
    [[ "$entry" != sha256:* && "$2" == $entry ]] && return 0
  done
  return 1
}

# The CBOM describes the final filesystem: Trivy applies later layers'
# deletions, so a key added in one layer and deleted in a later RUN is not in
# it, although the layer that holds it is still published. So private keys are
# looked for in each layer on its own, as a one-layer image sharing the
# layout's blobs. Trivy caches layer analysis by diff ID, so after the full scan
# this costs one quick Trivy run per layer.
# Writes "<fingerprint>\t<path>\t<name>\t<layer>" per key occurrence.
layer_private_keys() {  # <view-dir>
  local manifest config n i digest dir cdesc
  manifest="$(layout_blob "$1" "$(jq -r '.manifests[0].digest' "${1}/index.json")")"
  config="$(layout_blob "$1" "$(jq -r .config.digest <<< "$manifest")")"
  n="$(jq '.layers | length' <<< "$manifest")"
  for ((i = 0; i < n; i++)); do
    dir="${1}-layer${i}"
    mkdir -p "${dir}/blobs/sha256"
    cp "${1}/oci-layout" "${dir}/oci-layout"
    digest="$(jq -r --argjson i "$i" '.layers[$i].digest' <<< "$manifest")"
    ln -s "$(cd "${1}/blobs/sha256" && pwd -P)/${digest#sha256:}" "${dir}/blobs/sha256/${digest#sha256:}"
    jq -c --argjson i "$i" '.rootfs.diff_ids |= [.[$i]] | del(.history)' <<< "$config" > "${dir}/blob"
    cdesc="$(put_blob "$dir" "$(jq -c '.config.mediaType' <<< "$manifest")")"
    jq -c --argjson i "$i" --argjson c "$cdesc" '.layers |= [.[$i]] | .config = $c' <<< "$manifest" > "${dir}/blob"
    jq -n --argjson m "$(put_blob "$dir" "$(jq -c '.mediaType // "application/vnd.oci.image.manifest.v1+json"' <<< "$manifest")")" \
      '{schemaVersion: 2, mediaType: "application/vnd.oci.image.index.v1+json", manifests: [$m]}' > "${dir}/index.json"
    trivy image --quiet --exit-code 0 --scanners crypto --format cyclonedx --output "${dir}/cbom.json" --input "$dir" >&2 \
      || die "Trivy's crypto scan errored on layer ${digest}; the private key check cannot run. See log above."
    jq -r '.components[]? | select(.cryptoProperties.relatedCryptoMaterialProperties.type? == "private-key")
      | .name as $n | (.cryptoProperties.relatedCryptoMaterialProperties.fingerprint.content // "unknown") as $fp
      | .evidence.occurrences[]? | [$fp, .location, $n, $layer] | @tsv' --arg layer "$digest" "${dir}/cbom.json"
    rm -rf "$dir"
  done
}

# Moves <layout>/blob into the layout's blobs and prints its descriptor.
put_blob() {  # <layout-dir> <media-type-json>
  local d size
  d="$(sha256_of "${1}/blob")"
  size="$(wc -c < "${1}/blob" | tr -d ' ')"
  mv "${1}/blob" "${1}/blobs/sha256/${d}"
  jq -cn --argjson t "$2" --arg d "sha256:${d}" --argjson s "$size" '{mediaType: $t, digest: $d, size: $s}'
}

# Writes the platform's CBOM, gates on private keys and sets CRYPTO_SUMMARY
# (the step summary cell) and CRYPTO_BLOCKING (private keys not allowed).
crypto_inventory() {  # <view-dir> <platform>
  local cbom="${REPORT_DIR}/cbom-${2//\//-}.cdx.json" certs weak fp path name layer keys=0 allowed=0
  CRYPTO_BLOCKING=0
  trivy image --exit-code 0 --scanners crypto --format cyclonedx --output "$cbom" --input "$1" >&2 \
    || die "Trivy's crypto scan errored on ${2}; the private key check cannot run. See log above."
  certs="$(jq '[.components[]? | select(.cryptoProperties.assetType? == "certificate")] | length' "$cbom")"
  # One line per file holding a key, per layer.
  layer_private_keys "$1" > "${1}.keys"
  while IFS=$'\t' read -r fp path name layer; do
    [ -n "$path" ] || continue
    keys=$((keys + 1))
    if key_allowed "$fp" "$path"; then
      allowed=$((allowed + 1))
      log "${2}: allowed private key /${path} in layer ${layer} (${name}, sha256:${fp})"
    else
      CRYPTO_BLOCKING=$((CRYPTO_BLOCKING + 1))
      echo "::error title=Private key in image::${2}: /${path} in layer ${layer} is a private key (${name}, sha256:${fp}). Remove it from the step that adds it (deleting it in a later step leaves it in the published layer), or if it is a reviewed fixture add '${path}' (no leading slash) or 'sha256:${fp}' to trivy-allowed-private-keys." >&2
    fi
  done < "${1}.keys"
  # Weak: a key algorithm under 112 bits of classical security (NIST SP 800-57's
  # floor, e.g. RSA below 2048). Signature algorithms carry no level. Not gating.
  weak="$(jq -r '[.components[]? | select(.type == "cryptographic-asset"
                    and .cryptoProperties.assetType == "algorithm"
                    and (.cryptoProperties.algorithmProperties.classicalSecurityLevel // 1000) < 112)
                  | .name] | unique | join(", ")' "$cbom")"
  [ -n "$weak" ] && echo "::warning title=Weak keys in image::${2}: key algorithms below 112-bit security: ${weak}; see $(basename "$cbom") in the uploaded Trivy report." >&2
  CRYPTO_SUMMARY="${certs} certificates, ${keys} private keys"
  [ "$allowed" -gt 0 ] && CRYPTO_SUMMARY+=" (${allowed} allowed)"
  [ -n "$weak" ] && CRYPTO_SUMMARY+=", weak: ${weak}"
  return 0
}

{
  echo "### Trivy scan: \`${IMAGE_LABEL:-${REF##*@}}\`"
  echo
  echo "Threshold: \`${SEVERITY}\`, scanners: \`${SCANNERS}\`, vulnerabilities fail the gate: \`${FAIL_ON_VULNS}\`"
  echo
  echo "| Platform | Result | Secrets, misconfigurations, licenses | Vulnerabilities | Crypto inventory |"
  echo "|---|---|---|---|---|"
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"

failed=0
for ((p = 0; p < ${#PLATFORMS[@]}; p++)); do
  platform="${PLATFORMS[$p]}"
  report="${REPORT_DIR}/trivy-${platform//\//-}.json"
  log "scanning ${platform} ($(jq -r .digest <<< "${DESCRIPTORS[$p]}"))"
  vulns=0
  others=0
  if [ -n "$GATE_SCANNERS" ]; then
    status=0
    trivy image "${TRIVY_ARGS[@]}" "${JAVA_DB_HARBOR_ARGS[@]}" --output "$report" --input "${VIEWS}/${p}" || status=$?
    if [ "$status" -ne 0 ] && [ "${#JAVA_DB_HARBOR_ARGS[@]}" -gt 0 ]; then
      echo "::warning::Trivy errored scanning ${platform} with the Java DB from '${HARBOR}' (exit ${status}); retrying with Trivy's default Java DB repositories."
      JAVA_DB_HARBOR_ARGS=()
      status=0
      trivy image "${TRIVY_ARGS[@]}" --output "$report" --input "${VIEWS}/${p}" || status=$?
    fi
    [ "$status" -eq 0 ] || die "Trivy errored scanning ${platform} (exit ${status}); see log above."
    trivy convert --exit-code 0 --format table "$report"

    vulns="$(jq '[.Results[]?.Vulnerabilities // [] | length] | add // 0' "$report")"
    # Misconfigurations lists only failures unless --include-non-failures is set;
    # the Status filter keeps that true if it ever is.
    others="$(jq '[.Results[]? | (.Secrets // []), [.Misconfigurations[]? | select(.Status == "FAIL")], (.Licenses // []) | length] | add // 0' "$report")"
  fi
  crypto="off"
  CRYPTO_BLOCKING=0
  if scanner_on crypto; then
    crypto_inventory "${VIEWS}/${p}" "$platform"
    crypto="$CRYPTO_SUMMARY"
  fi
  blocking=$((others + CRYPTO_BLOCKING))
  [ "$FAIL_ON_VULNS" = true ] && blocking=$((blocking + vulns))
  if [ "$blocking" -gt 0 ]; then
    result="**FAIL**"
    failed=1
  else
    result="pass"
  fi
  if [ "$vulns" -gt 0 ] && [ "$FAIL_ON_VULNS" != true ]; then
    echo "::warning title=Trivy vulnerabilities (not gating)::${platform}: ${vulns} ${SEVERITY} vulnerabilities; see the scan log or the uploaded Trivy report."
  fi
  echo "| \`${platform}\` | ${result} | ${others} | ${vulns} | ${crypto} |" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
done

if [ "$failed" -ne 0 ]; then
  what="secret, misconfiguration or license"
  [ "$FAIL_ON_VULNS" = true ] && what="secret, misconfiguration, license or vulnerability"
  die "Trivy found ${SEVERITY} ${what} findings or a private key. The image has NOT been pushed to any registry. Fix the findings, or add a reviewed exception to the trivy-ignorefile (private keys: trivy-allowed-private-keys)."
fi
log "Trivy scan passed for all platforms."
