#!/usr/bin/env bash
# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
fail() { echo "::error::$1" >&2; exit 1; }
[[ "${RUNNER_OS:-}" == Linux ]] || fail 'This action currently supports Linux runners.'
for tool in curl jq; do
  command -v "$tool" >/dev/null || fail "Required tool is missing: $tool"
done
[[ "$VAULT_ROLE" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'role must contain only letters, digits, underscores, or hyphens.'
[[ "$VAULT_TOKEN_NAME" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'token-name must contain only letters, digits, underscores, or hyphens.'
# The OIDC JWT and Vault token go to this endpoint. Reject non-HTTPS, credentials,
# queries, fragments, and embedded path components. Configure only trusted hosts.
[[ "$VAULT_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?$ ]] || fail 'vault-url must be a trusted HTTPS origin, without credentials or a path.'
[[ -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" && -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]] || fail 'The calling job must grant id-token: write.'
proxy="$PROXY_URL"
daemon_args=''
join=false
case "$NETWORK_MODE" in
  userspace)
    proxy="${proxy:-http://127.0.0.1:1055}"
    [[ "$proxy" =~ ^http://(127\.0\.0\.1|localhost):([0-9]{1,5})$ ]] || fail 'userspace proxy-url must be http://127.0.0.1:PORT or http://localhost:PORT.'
    port="${BASH_REMATCH[2]}"
    (( 10#$port >= 1 && 10#$port <= 65535 )) || fail 'Proxy port is out of range.'
    listen="${proxy#http://}"
    daemon_args="--tun=userspace-networking --socks5-server=$listen --outbound-http-proxy-listen=$listen"
    ;;
  kernel)
    [[ -z "$proxy" ]] || fail 'proxy-url is only supported in userspace or existing mode.'
    ;;
  existing)
    [[ -z "$proxy" || "$proxy" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?$ ]] || fail 'proxy-url must be an HTTP(S) origin without credentials or a path.'
    ;;
  *) fail 'network-mode must be userspace, kernel, or existing.' ;;
esac
if [[ "$NETWORK_MODE" != existing ]]; then
  # A second token request can reuse the first connection. An existing connection
  # is borrowed, never logged out here; only a Join step registers post cleanup.
  if ! command -v tailscale >/dev/null || ! tailscale status >/dev/null 2>&1; then
    [[ -n "$TS_CLIENT_ID" && -n "$TS_AUDIENCE" ]] || fail 'Joining Tailscale requires tailscale-oauth-client-id and tailscale-audience.'
    join=true
  fi
fi
{
  echo "url=${VAULT_URL%/}"
  echo "proxy=$proxy"
  echo "join=$join"
  echo "tailscaled-args=$daemon_args"
} >> "$GITHUB_OUTPUT"
