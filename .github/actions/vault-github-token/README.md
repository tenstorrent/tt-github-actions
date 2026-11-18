# Vault GitHub token

Get a short-lived GitHub token using the calling job's GitHub OIDC identity.
This action centralizes the OSPO Vault URL, JWT mount, Tailscale action pin,
connection setup, bounded HTTP requests, and token masking.

It supports GitHub-hosted Linux runners, self-hosted Linux runners, and Linux
runner pods without `/dev/net/tun` or `NET_ADMIN`. Runners need Bash, curl, and jq.
Creating a Tailscale connection also requires the prerequisites of the pinned
Tailscale action (including sudo); userspace mode removes the TUN requirement,
not all runner setup requirements. Windows and macOS are not supported yet.

## Usage from any authorized repository

```yaml
permissions:
  contents: read
  id-token: write

jobs:
  example:
    runs-on: ubuntu-latest
    steps:
      - name: Get GitHub token
        id: vault
        timeout-minutes: 5
        # Replace REVIEWED_COMMIT_SHA with a reviewed full commit.
        uses: tenstorrent/tt-github-actions/.github/actions/vault-github-token@REVIEWED_COMMIT_SHA
        with:
          role: ${{ vars.VAULT_ROLE }}
          token-name: ${{ vars.VAULT_TOKEN_NAME }}
          tailscale-oauth-client-id: ${{ vars.TS_VAULT_OSPO_CLIENT_ID }}
          tailscale-audience: ${{ vars.TS_VAULT_OSPO_AUDIENCE }}
      - name: Use the token
        env:
          GH_TOKEN: ${{ steps.vault.outputs.token }}
        run: gh api installation/repositories --jq '.repositories[].full_name'
```

The action does not require checkout. Its caller retains its own repository/ref/
event identity: hosting the action here grants no Vault access to other repos.
Keep each caller's trusted-event and branch conditions around token acquisition
and its consuming steps. Vault policies and Tailscale federation enforce access.

## Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `role` | Required | Existing role under Vault's `jwt-github` auth mount. |
| `token-name` | Required | Existing name beneath `github/token/`, without the prefix. |
| `vault-url` | OSPO production URL | Trusted HTTPS Vault origin; also the OIDC audience. Supports staging or another compatible Vault deployment. |
| `network-mode` | `userspace` | `userspace`, `kernel`, or `existing`; details below. |
| `proxy-url` | See below | Proxy for Vault requests only. |
| `tailscale-oauth-client-id` | Empty | Tailscale OIDC federation ID; required if a new connection is needed. Not a secret. |
| `tailscale-audience` | Empty | Tailscale OIDC audience; required if a new connection is needed. |
| `tailscale-tags` | `tag:tt-gha-vault-ospo` | Tags used for a new connection. |

Only use reviewed, trusted `vault-url` and `proxy-url` values. The Vault origin
receives the job's OIDC JWT and subsequent Vault credentials. The action expects
the same JWT mount and GitHub token response contract as OSPO Vault.

## Networking and connection ownership

- **userspace** (default): reaches Vault through a loopback HTTP proxy, without
  TUN or `NET_ADMIN`. Works for both GitHub-hosted Ubuntu and exabox-style Linux
  pods. `proxy-url` defaults to `http://127.0.0.1:1055`; override it with another
  loopback port if needed. Every Vault request explicitly uses this proxy, even
  if `NO_PROXY` would otherwise bypass it. GitHub OIDC requests use normal runner
  networking. Authentication uses curl in all modes, avoiding a separate Vault
  action implementation that cannot use the userspace proxy.
- **kernel**: uses Tailscale routes. Requires the runner's TUN/network privileges.
  `proxy-url` must be empty.
- **existing**: creates and manages no Tailscale connection. Use this for runners
  that already reach Vault via a VPN, private network, or existing proxy. Set
  `proxy-url` explicitly if needed; empty means direct access. No Tailscale client
  ID or audience is required in this mode.

In userspace/kernel mode an active Tailscale connection is reused. It must already
provide the requested route/proxy; this action will not reconfigure a borrowed
connection. A mismatched proxy fails the readiness check. This permits multiple
calls in one job, for example separate tt-orchestration and tt-d-gen tokens.

A connection created by this action remains available until the pinned Tailscale
action's automatic job-end cleanup. Borrowed connections are not logged out.
Remove redundant caller logout steps; they can interrupt later token requests.
The upstream post hook runs after ordinary failures too, but abrupt runner loss
can prevent cleanup. Disconnecting does not revoke issued tokens; their scopes
and lifetimes are controlled by Vault/GitHub configuration.

## Outputs and failures

`token` is a masked step output. Use it only in a consuming step's `with` or `env`,
not interpolated into shell source or exported job-wide. The action does not
export the Vault login token. Do not move credentials across jobs via outputs.

Missing configuration, denied access, unreachable Vault, malformed responses,
and empty/multiline tokens fail the action without producing a GitHub token.
Only a test harness should use `continue-on-error`; it must skip token-dependent
checks when acquisition failed and report them as untested.

Vault readiness has six bounded attempts; sealed/uninitialized responses are not
accepted as healthy. GitHub OIDC requests have bounded transient retries. Login
and token issuance have 30-second limits and no automatic retries to avoid
minting additional credentials after a lost response. Set a step timeout as in
the example to also bound network setup.

## Migrating existing callers

For [tt-mlir #9400](https://github.com/tenstorrent/tt-mlir/pull/9400), replace Join
Tailscale / Wait / Get token with this action, keeping schedule/main conditions
and the downstream `workflow_run` guard. The role/token names stay unchanged.
Consume `steps.vault.outputs.token` and remove the repeated Vault URL and logout.

For [tt-shield #1448](https://github.com/tenstorrent/tt-shield/pull/1448), replace
both the inline userspace/curl blocks and the repository-local Vault wrapper.
The input names match its role/token/Tailscale inputs. Userspace is the default
for both hosted and exabox jobs; multiple tokens can share one connection.
Existing repository contract tests that insist on inline proxy commands need
updating to recognize the pinned shared action.

Pin this action to a reviewed full SHA. Dependency updates happen here once;
consumers still update their wrapper SHA. Token-consuming actions are separate.

## Tests

```sh
python3 -m unittest discover -s .github/actions/vault-github-token/tests -v
shellcheck .github/actions/vault-github-token/scripts/*.sh
```

Tests use fake credentials and mock all HTTP requests. They cover each network
mode, connection reuse, proxy routing, request bounds, input validation, denied
or malformed responses, and masking/output handling. Live Tailscale/Vault access
still needs a provisioned caller and an authorized integration run.
