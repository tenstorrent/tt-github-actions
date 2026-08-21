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

| Key | Required | Description |
|-----|----------|-------------|
| `model` | yes | LLM for classification; `"none"` skips it. |
| `workspace` | yes | Base for relative paths. Use the `$GITHUB_WORKSPACE` env var — `${{ github.workspace }}` is the host path, which container jobs don't have. |
| `input_dirs` | yes | Directories holding the `.log` files to analyze. |
| `output_dir` | yes | Where the summary and prompt are written. |
| `scope` | no | Set when the calling workflow runs more than once per run, so each report covers only its own legs. Pass the same value to `ai_summary/run`. |
| `authoritative_job_status` | no | `true` lets a green job clear the crash/timeout patterns; see below. |
| `log_start_marker`, `log_complete_marker` | no | Regexes for run-with-log's sentinels; see below. |

Analysis fields (`layers`, `categories`, `test_patterns`,
`failed_test_patterns`, `detection_patterns`, `repos`) default to the bundled
[`analysis.yaml`](../tool/ai_job_summary/config/analysis.yaml). Supplying one
**extends** it — lists append, dicts deep-merge — so a project can add but not
remove. Only `"layers_mode": "replace"` swaps a field out. `tool_dir` is
rejected; any other key passes through untouched.

### Authoritative job status

A `crash` or `timeout` pattern anywhere in the log sets the status by itself, wrong when
the token does not mean the process died — a gtest negative test catching its own
`TT_FATAL` logs one and passes. `"authoritative_job_status": true` lets a green
`job.status` clear both; failed tests, a non-zero exit code and a missing finish marker
still decide. Opt-in: green only means nothing that *could* fail it did.

A misspelled config key is silent, so the tool logs `Job status is authoritative:
<status>` when it takes effect.

### Log sentinels

Defaults match `[==tt-log-start-line==]` / `[==tt-log-finish-line==]` (the
latter with an optional `exit_code=N` in group 1). A log carrying the start
sentinel but not the finish one was hard-killed mid-run — the GitHub
`timeout-minutes` kill, invisible in the log itself — so it classifies as
TIMEOUT instead of a false SUCCESS. Logs without the start sentinel (e.g. a
backgrounded server's tail) are untracked, so non-adopters need no opt-out. A
crash/failure already in the log wins, with the truncation flagged as
`log_complete: false`.

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
