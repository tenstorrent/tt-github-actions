# ai_summary/job

Analyzes CI job logs with an LLM and produces a structured per-job summary
(`.md` + `.json`).

> **Runtime:** the action installs into an active `$VIRTUAL_ENV` if one
> exists (typical for Docker container jobs whose image pre-creates a
> venv at `/opt/venv`). On bare self-hosted runners with no pre-set venv,
> the action falls back to building one in `/tmp/ai-summary/venv` —
> ephemeral, no action-cache footprint.

## Usage

```yaml
- uses: tenstorrent/tt-github-actions/.github/actions/ai_summary/job@main
  if: always() && !cancelled()
  continue-on-error: true
  with:
    config: |
      {
        "model": "anthropic/claude-sonnet-4-5-20250929",
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
  "model": "anthropic/claude-sonnet-4-5-20250929",
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

Categories, layers, and analysis patterns come from the bundled
`analysis.yaml` shipped with the tool.

## Outputs

| Name | Description |
|------|-------------|
| `summary-dir` | Directory containing `ai_job_summary_<job-id>.md` and `.json` |
