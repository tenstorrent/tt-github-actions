# ai_summary/job

Analyzes CI job logs with an LLM and produces a structured per-job summary
(`.md` + `.json`).

> **Runtime:** uses `$VIRTUAL_ENV` if set, otherwise an ephemeral venv at `/tmp/ai-summary/venv`.

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
| `config` | yes | JSON config string (see "Config schema" below). |
| `api-key` | yes | LLM API key (secret). |
| `api-url` | yes | LLM API URL. |
| `job-name` | no | Job name used in the summary header and stamped as `_job.name` in the output JSON. Must match the corresponding entry in `expected-jobs` (passed to `ai_summary/run`) for INFRA_FAILURE reconciliation to work. Defaults to `job.name`. |

## Config schema

The action takes inline JSON — no separate config file. Required fields:

```json
{
  "model": "${{ vars.AI_SUMMARY_MODEL }}",
  "workspace": "$GITHUB_WORKSPACE",
  "input_dirs": ["generated/test_logs"],
  "output_dir": "generated/ai_summaries"
}
```

- `model` — LLM for classification (or `"none"` to skip the LLM).
- `workspace` — base directory for relative paths. Use `$GITHUB_WORKSPACE`
  (the env var, expanded at runtime — the `${{ github.workspace }}`
  expression resolves to the host path, which doesn't exist inside
  container jobs). For repos checked out into a subdir use
  `$GITHUB_WORKSPACE/docker-job`. Absolute paths in
  `input_dirs`/`output_dir` pass through unchanged.
- `input_dirs` — directories with `.log` files to analyze.
- `output_dir` — where to write the per-job summary.
- `log_start_marker` / `log_complete_marker` (optional overrides) — regexes
  for run-with-log's start/finish sentinels (defaults match
  `[==tt-log-start-line==]` / `[==tt-log-finish-line==]`, the latter with an
  optional `exit_code=N` in group 1). A log carrying the start sentinel but
  not the finish one was hard-killed mid-run — the GitHub `timeout-minutes`
  kill, invisible in the log itself — so it classifies as TIMEOUT instead of a
  false SUCCESS. Logs without the start sentinel (e.g. a backgrounded server's
  tail) are untracked, so non-adopters need no opt-out. A crash/failure
  already in the log wins, with the truncation flagged as
  `log_complete: false`.

Categories, layers, and analysis patterns come from the bundled
`analysis.yaml` shipped with the tool.

## Outputs

| Name | Description |
|------|-------------|
| `summary-dir` | Directory containing `ai_job_summary_<job-id>.md` and `.json` |
