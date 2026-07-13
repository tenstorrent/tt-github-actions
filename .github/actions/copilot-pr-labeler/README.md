# Copilot PR Type Labeler

Automatically labels **merged** pull requests with a single change-type label, chosen by the
[GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions)
running inside GitHub Actions. Copilot usage is billed to the organization via existing Copilot
seats — **no separate LLM API key or PAT is required.**

The classifier is given the PR title, description, changed-file list, and a (truncated) diff, and
picks exactly one [Conventional Commits](https://www.conventionalcommits.org/) type.

## What it does

- Fires when a PR is **merged** (not on plain close, not on `push`).
- Adds **exactly one** label from the fixed taxonomy below.
- Creates the label in the target repo on demand if it doesn't already exist (existing labels,
  including any custom colors you've set, are left untouched).
- Forward-only: it labels new merges going forward. There is **no backfill** of historical PRs.

## Label taxonomy

Exactly one of these 11 Conventional Commits types is applied per PR:

```
feat      New feature
fix       Bug fix
docs      Documentation only
style     Formatting, no code-behavior change
refactor  Code change that neither fixes a bug nor adds a feature
perf      Performance improvement
test      Adds or corrects tests
build     Build system, dependencies, or packaging
ci        CI configuration and scripts
chore     Routine maintenance
revert    Reverts a previous change
```

## Prerequisites

- **Org policy (one-time, org admin):** *"Allow use of Copilot CLI billed to the organization"*
  must be enabled in the organization's Copilot settings. Without it, the Copilot request fails and
  the action applies the `fallback_label` (default `chore`) with a warning in the run log.
- The calling workflow must grant `copilot-requests: write` (for Copilot) and `pull-requests: write`
  (to apply the label). See the example below.

## Usage

Add a workflow like this to the adopting repository (see
[`example-workflow.yml`](./example-workflow.yml) for the copy-paste version):

```yaml
name: Label merged PRs by type

on:
  pull_request:
    types: [closed]

permissions:
  contents: read
  pull-requests: write
  copilot-requests: write

jobs:
  label:
    if: github.event.pull_request.merged == true
    runs-on: ubuntu-latest
    steps:
      - name: Classify and label merged PR
        uses: tenstorrent/tt-github-actions/.github/actions/copilot-pr-labeler@main
```

That's the whole adoption — no checkout, no secrets, no inputs required.

## Inputs

| Input             | Default              | Description                                                                                         |
| ----------------- | -------------------- | --------------------------------------------------------------------------------------------------- |
| `gh_token`        | `${{ github.token }}`| Token for `gh` label ops and Copilot auth. Needs `pull-requests:write` + `copilot-requests:write`.  |
| `max_ai_credits`  | `50`                 | Soft per-run cap on Copilot AI credits (`copilot --max-ai-credits`). GitHub recommends above 30.    |
| `fallback_label`  | `chore`              | Label applied if Copilot is unavailable or returns no valid type. Must be one of the 11 types.      |
| `node_version`    | `22`                 | Node.js version used to install the Copilot CLI (requires 22+).                                     |

## Cost control

Because this fires on every PR merge, each run passes `--max-ai-credits` to the Copilot CLI as a
soft per-run cap ([session limits](https://docs.github.com/copilot/how-tos/copilot-cli/use-copilot-cli/set-session-limit)).
The diff is truncated to ~40 KB and the description to ~4 KB before it reaches the model, keeping
each classification to a single small request.

## Fork PRs (limitation)

For PRs opened from **forks**, the `pull_request` event's `GITHUB_TOKEN` is read-only regardless of
the `permissions:` block, so the label cannot be applied. This covers most external-community PRs.
Same-repo (org-member) PRs — the common case in Tenstorrent repos — are unaffected. Repos that need
fork coverage can switch the caller's trigger to `pull_request_target` (which runs with a writable
token in the base-repo context); this is safe here because the action only classifies text and never
executes PR code, but adopt it deliberately.

## How classification is kept safe

PR title/body are attacker-influenceable text, so they are passed to the shell via environment
variables (never interpolated inline) and the model's answer is validated against the fixed 11-item
allowlist. The worst a prompt-injection attempt can achieve is a wrong label from that fixed set —
it cannot change what the action does.
