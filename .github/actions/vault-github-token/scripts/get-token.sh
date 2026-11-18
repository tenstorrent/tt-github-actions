#!/usr/bin/env bash
# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

# Never trace credentials, even if the caller enables shell tracing.
set +x
set -euo pipefail
# Explicit proxy selection prevents NO_PROXY from bypassing userspace routing.
# GitHub's OIDC endpoint uses the runner's normal networking, not this proxy.
vault_curl=(curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --proxy "$VAULT_PROXY" --noproxy '')
ready=false
for attempt in 1 2 3 4 5 6; do
  if "${vault_curl[@]}" --max-time 5 "$VAULT_URL/v1/sys/health?standbyok=true" >/dev/null; then
    ready=true
    break
  fi
  if [[ "$attempt" -lt 6 ]]; then sleep 2; fi
done
if [[ "$ready" != true ]]; then
  echo '::error::Vault did not become ready through the selected connection.' >&2
  exit 1
fi
# Require nonempty, single-line strings before masking or writing to GITHUB_OUTPUT.
selector='select(type == "string" and length > 0 and (test("[\\r\\n]") | not))'
mask() {
  local value="$1"
  value="${value//%/%25}"
  echo "::add-mask::$value"
}
audience=$(jq -rn --arg value "$VAULT_URL" '$value | @uri')
separator='&'
[[ "$ACTIONS_ID_TOKEN_REQUEST_URL" == *\?* ]] || separator='?'
jwt=$(curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
  --retry 2 --retry-delay 1 --retry-max-time 90 \
  -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}${separator}audience=$audience" | jq -er ".value | $selector")
mask "$jwt"
# No automatic retries for login or token issuance: a lost response could have
# created a credential already. Each request has a strict time limit.
vault_token=$(jq -n --arg role "$VAULT_ROLE" --arg jwt "$jwt" '{role:$role,jwt:$jwt}' |
  "${vault_curl[@]}" -X POST --data @- "$VAULT_URL/v1/auth/jwt-github/login" |
  jq -er ".auth.client_token | $selector")
mask "$vault_token"
github_token=$("${vault_curl[@]}" -H "X-Vault-Token: $vault_token" \
  "$VAULT_URL/v1/github/token/$VAULT_TOKEN_NAME" | jq -er ".data.token | $selector")
mask "$github_token"
printf 'token=%s\n' "$github_token" >> "$GITHUB_OUTPUT"
