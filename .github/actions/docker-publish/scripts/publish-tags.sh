#!/bin/bash
# Point every requested tag at the scanned + enriched index. Runs only after all
# gates pass. Work is split into two phases so that no tag exists until every
# copy it refers to is in place:
#   stage: copy the index by digest, with no tag, to every repository other than
#          the primary via `oras cp` (images + attestations, so the digest is
#          identical everywhere), then attach the signed SBOM bundles to it.
#   tag:   apply the requested tags to the index in every repository, then check
#          that each tag resolves to the index digest.
# PUBLISH_PHASE selects the phase. `all` (default) runs stage, then tag. A tag-only
# run needs an earlier stage run for the same digest. The tag phase is one registry
# call per repository, which registries cannot make atomic, so a failure there can
# leave some repositories tagged.
#
# Usage: publish-tags.sh <primary-repo>@<index-digest>
# Env:   INPUT_TAGS (newline separated fully-qualified name:tag; validated by
#        prepare.sh or bake.sh), ATTEST_SBOM (default true: copies to other
#        repositories must include a signed SBOM bundle for every platform),
#        PUBLISH_PHASE (stage, tag or all; default all)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SRC="${1:?Usage: $0 <primary-repo>@<index-digest>}"
parse_ref "$SRC"
PRIMARY_REPO="$REF_REPO"
DIGEST="$REF_DIGEST"
PUBLISH_PHASE="${PUBLISH_PHASE:-all}"
case "$PUBLISH_PHASE" in
  stage | tag | all) ;;
  *) die "PUBLISH_PHASE must be stage, tag or all; got '${PUBLISH_PHASE}'." ;;
esac

# Signed SBOM bundles (attest-sbom.sh) are referrers of the index and platform
# images, so a plain copy of the index leaves them behind. They are copied in
# oras's auto mode, unlike the index: on a registry without the Referrers API
# a bundle is only discoverable through the sha256-<digest> fallback tag, which
# is also what actions/attest creates in the primary repository.
#
# Registry output is captured into variables before looping: a failing command
# substitution in a `for` word list would not trip `set -e`.
SBOM_BUNDLES=()  # "<subject> <bundle-digest>"
# attest-sbom.sh signs one bundle per platform image, so with ATTEST_SBOM every
# platform must have one: a missing platform would publish copies that lack its
# signed SBOM. Any Sigstore bundle is copied, but only one whose signed DSSE
# payload has the SPDX predicate type counts as the SBOM: a provenance or other
# bundle on the same digest (a reproducible build attested elsewhere) must not
# stand in for it. A referrer can take a moment to become discoverable after
# actions/attest pushes it, so discovery is retried before failing.
SBOM_DISCOVERY_ATTEMPTS=5
SBOM_DISCOVERY_DELAY_SECONDS=10
discover_bundles() {  # <subject> -> bundle digests, one per line
  local referrers
  referrers="$(oras discover --format json --artifact-type "$SIGSTORE_BUNDLE_TYPE" "${PRIMARY_REPO}@$1")"
  printf '%s' "$referrers" | jq -r '.referrers[]?.digest'
}
# Reads the predicate type from the signed payload, not the unsigned
# dev.sigstore.bundle.predicateType annotation. An unreadable bundle is not an
# SBOM; the missing-platform check then retries and fails.
is_sbom_bundle() {  # <bundle-digest>
  local manifest layer payload
  manifest="$(oras manifest fetch "${PRIMARY_REPO}@$1")" || return 1
  layer="$(printf '%s' "$manifest" | jq -r '.layers[0].digest // empty')"
  [ -n "$layer" ] || return 1
  payload="$(oras blob fetch --output - "${PRIMARY_REPO}@${layer}")" || return 1
  [ "$(printf '%s' "$payload" | jq -r '.dsseEnvelope.payload // empty | @base64d | fromjson | .predicateType // empty' 2>/dev/null)" \
    = "$SPDX_PREDICATE_TYPE" ]
}
find_sbom_bundles() {
  local index platforms subject refs ref attempt missing sbom
  index="$(oras manifest fetch "$SRC")"
  platforms="$(printf '%s' "$index" | jq -r '.manifests[] | select(.platform.os != "unknown") | .digest')"
  [ -n "$platforms" ] || die "No platform images found in ${SRC}."
  for ((attempt = 1; ; attempt++)); do
    SBOM_BUNDLES=()
    missing=()
    while IFS= read -r subject; do
      [ -n "$subject" ] || continue
      refs="$(discover_bundles "$subject")"
      sbom=false
      while IFS= read -r ref; do
        [ -n "$ref" ] || continue
        SBOM_BUNDLES+=("${subject} ${ref}")
        [ "$sbom" = true ] || ! is_sbom_bundle "$ref" || sbom=true
      done <<< "$refs"
      [ "$sbom" = true ] || [ "$subject" = "$DIGEST" ] || missing+=("$subject")
    done <<< "${DIGEST}"$'\n'"${platforms}"
    [ "${ATTEST_SBOM:-true}" = true ] && [ "${#missing[@]}" -gt 0 ] || return 0
    [ "$attempt" -lt "$SBOM_DISCOVERY_ATTEMPTS" ] \
      || die "No signed SBOM bundle (predicate ${SPDX_PREDICATE_TYPE}) found as a referrer of platform image(s) ${missing[*]} of ${SRC}; refusing to publish copies without them."
    log "signed SBOM bundle not yet discoverable for ${missing[*]}; retrying in ${SBOM_DISCOVERY_DELAY_SECONDS}s (attempt ${attempt}/${SBOM_DISCOVERY_ATTEMPTS})"
    sleep "$SBOM_DISCOVERY_DELAY_SECONDS"
  done
}

copy_sbom_bundles() {  # <dest-repo>
  local dest="$1" entry subject ref
  for entry in "${SBOM_BUNDLES[@]}"; do
    subject="${entry%% *}"
    ref="${entry#* }"
    oras cp "${PRIMARY_REPO}@${ref}" "${dest}@${ref}" >&2
    log "copied signed bundle ${ref} (subject ${subject}) to ${dest}"
  done
}

declare -A TAGS_BY_REPO=()
REPOS=()
while IFS= read -r t; do
  [ -n "$t" ] || continue
  parse_ref "$t"
  [ -n "${TAGS_BY_REPO[$REF_REPO]+x}" ] || REPOS+=("$REF_REPO")
  TAGS_BY_REPO[$REF_REPO]="${TAGS_BY_REPO[$REF_REPO]:+${TAGS_BY_REPO[$REF_REPO]},}${REF_TAG}"
done <<< "${INPUT_TAGS}"

# Stage: look the bundles up before copying anything, so a missing bundle or a
# registry error fails before any copy is made. Then copy the index and its
# bundles to each other repository by digest. No tag is created here.
if [ "$PUBLISH_PHASE" != tag ]; then
  for repo in "${REPOS[@]}"; do
    if [ "$repo" != "$PRIMARY_REPO" ]; then
      find_sbom_bundles
      break
    fi
  done

  for repo in "${REPOS[@]}"; do
    [ "$repo" != "$PRIMARY_REPO" ] || continue
    # No destination reference: oras copies the graph and creates no tag.
    oras cp --to-distribution-spec "$ORAS_REFERRERS_SPEC" "${SRC}" "${repo}" >&2
    copy_sbom_bundles "$repo"
  done
fi

# Tag: apply the requested tags in every repository (the primary included), then
# confirm each tag resolves to the index digest.
if [ "$PUBLISH_PHASE" != stage ]; then
  for repo in "${REPOS[@]}"; do
    IFS=',' read -r -a tags <<< "${TAGS_BY_REPO[$repo]}"
    oras tag "${repo}@${DIGEST}" "${tags[@]}" >&2
    for tag in "${tags[@]}"; do
      resolved="$(oras resolve "${repo}:${tag}")"
      [ "$resolved" = "$DIGEST" ] || die "${repo}:${tag} resolved to ${resolved}, expected ${DIGEST}."
      log "published ${repo}:${tag}@${DIGEST}"
      echo "- \`${repo}:${tag}@${DIGEST}\`" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    done
  done
fi
