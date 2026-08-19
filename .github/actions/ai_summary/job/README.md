# ai_summary/job

Analyzes CI job logs with an LLM and produces a structured per-job summary
(`.md` + `.json`).

> **Runtime:** runs in an ephemeral venv at `/tmp/ai-summary/venv`.

## Usage

```yaml
- uses: tenstorrent/tt-github-actions/.github/actions/ai_summary/job@main
  if: always() && !cancelled()
  continue-on-error: true
  with:
    config: |
      {
        "model": "${{ vars.AI_SUMMARY_MODEL }}",
        "workspace": "$GITHUB_WORKSPACE",
        "input_dirs": ["generated/test_logs"],
        "output_dir": "generated/ai_summaries"
      }
    api-key: ${{ secrets.TT_CHAT_API_KEY }}
    api-url: ${{ secrets.TT_CHAT_URL }}
    job-name: ${{ matrix.test-group.name }}  # optional; defaults to job.name
```

## Inputs

| Name | Required | Description |
|------|---|-------------|
| `config` | yes | JSON config string. Keys are listed under [Config schema](#config-schema). |
| `api-key` | yes | LLM API key (secret). |
| `api-url` | yes | LLM API URL. |
| `job-name` | no | Job name used in the summary header and stamped as `_job.name` in the output JSON. Must match the corresponding entry in `expected-jobs` (passed to `ai_summary/run`) for INFRA_FAILURE reconciliation to work. Defaults to `job.name`. |

## Config schema

The action takes inline JSON — no separate config file.

```json
{
  "model": "${{ vars.AI_SUMMARY_MODEL }}",
  "workspace": "$GITHUB_WORKSPACE",
  "input_dirs": ["generated/test_logs"],
  "output_dir": "generated/ai_summaries"
}
```

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `model` | yes | — | LLM used for classification. `"none"` skips the LLM entirely. |
| `workspace` | yes | — | Base directory for relative paths. See the note below. |
| `input_dirs` | yes | — | List of directories holding `.log` files to analyze. |
| `output_dir` | yes | — | Directory the per-job summary and prompt are written to. |
| `scope` | no | `""` | Discriminator for one invocation of a reusable workflow. Set it when the calling workflow runs more than once per run, and pass the **same value** to `ai_summary/run`. See the note below. |
| `log_start_marker` | no | run-with-log's sentinel | Regex for the log's start sentinel. |
| `log_complete_marker` | no | run-with-log's sentinel | Regex for the log's finish sentinel, with an optional `exit_code=N` in group 1. |

Analysis fields — `layers`, `categories`, `test_patterns`,
`failed_test_patterns`, `detection_patterns`, `repos` — default to the bundled
[`analysis.yaml`](../tool/ai_job_summary/config/analysis.yaml); read it for the
shape of each. Supplying one **extends** the bundled value rather than replacing
it: lists are appended and dicts deep-merged, so a project can add a category or
a pattern but cannot remove one. The single exception is `layers`, which
`"layers_mode": "replace"` swaps out wholesale.

`tool_dir` is rejected — the action resolves the tool from its own location.
Any other key is passed through to the config dict untouched.

### scope

A workflow invoked more than once per run (a platform matrix on the `uses:`
line, say) produces one set of per-leg artifacts per invocation, all of them
visible to every sibling. `scope` partitions them: it is added to the artifact
and file names, and recorded in the summary JSON as `_job.scope`. `ai_summary/run`
matches on that recorded value rather than on the filename, so a mismatch
between the two is reported rather than silently mixing invocations.

Leave it unset for a workflow that runs once per run; names and behaviour are
then unchanged.

### workspace

Use `$GITHUB_WORKSPACE` — the env var, expanded at runtime. The
`${{ github.workspace }}` expression resolves to the host path, which doesn't
exist inside container jobs. For repos checked out into a subdir use
`$GITHUB_WORKSPACE/docker-job`. Absolute paths in `input_dirs` / `output_dir`
pass through unchanged.

### Log sentinels

Defaults match `[==tt-log-start-line==]` / `[==tt-log-finish-line==]`. A log
carrying the start sentinel but not the finish one was hard-killed mid-run —
the GitHub `timeout-minutes` kill, invisible in the log itself — so it
classifies as TIMEOUT instead of a false SUCCESS. Logs without the start
sentinel (e.g. a backgrounded server's tail) are untracked, so non-adopters
need no opt-out. A crash/failure already in the log wins, with the truncation
flagged as `log_complete: false`.

## Outputs

| Name | Description |
|------|-------------|
| `summary-dir` | Directory containing the `.md` and `.json` summary and the `.txt` prompt dump |

Two artifacts are uploaded, both stemmed
`<kind>[_<scope>]_r<run>_a<attempt>_j<job>`: `ai_job_summary_…` (the `.md` and
`.json`, which `ai_summary/run` aggregates) and `ai_job_prompt_…` (the exact
prompt sent to the LLM, kept for diagnosing a wrong verdict once the runner is
gone). The `scope` segment is present only when `scope` is set in the config.

Names carry the run attempt because artifact names are unique per run, not per
attempt — a re-run would otherwise overwrite the attempt it replaced. Several
attempts coexist on one run: a partial re-run deletes nothing, and a full
re-run does not reliably purge. Consumers should match the prefix and take the
highest attempt rather than assume one file per job.
