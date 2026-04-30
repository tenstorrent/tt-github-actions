# ai_summary/run

Downloads per-job AI summaries (produced by `ai_summary/job`) and aggregates
them into one run-level report. Optionally renders the report as a PNG and
posts it to Slack.

## Usage

```yaml
ai-run-summary:
  needs: [generate-matrix, your-matrix-job]
  if: always()
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - id: summary
      uses: tenstorrent/tt-github-actions/.github/actions/ai_summary/run@main
      with:
        config: |
          {
            "model": "anthropic/claude-sonnet-4-5-20250929",
            "workspace": "$GITHUB_WORKSPACE",
            "input_dir": "ai_job_summaries",
            "output_dir": "ai_run_summaries"
          }
        api-key: ${{ secrets.TT_CHAT_API_KEY }}
        api-url: ${{ secrets.TT_CHAT_URL }}
        # Pass these two together to surface INFRA_FAILURE rows for matrix
        # legs that produced no summary (runner died, container setup failed):
        expected-jobs: ${{ needs.generate-matrix.outputs.matrix }}
        run-result:    ${{ needs.your-matrix-job.result }}
        # Optional Slack delivery — omit both to skip.
        slack-bot-token: ${{ secrets.SLACK_BOT_TOKEN }}
        slack-channel-id: ${{ secrets.SLACK_CHANNEL_ID }}
```

`expected-jobs` and `run-result` must be passed together (or both omitted);
passing only one is a hard error. When `run-result` is `cancelled` or
`skipped`, no synthesis is performed.

## Inputs

| Name | Required | Default | Description |
|------|---|---|-------------|
| `config` | yes | — | JSON config string. See "Config schema" below. |
| `api-key` | no | `""` | LLM API key. Set `"model": "none"` in config to skip the narrative LLM call. |
| `api-url` | no | `""` | LLM API URL. |
| `expected-jobs` | no | `""` | JSON array of expected matrix legs (typically `needs.<matrix-job>.outputs.matrix`). When set with `run-result`, the action synthesizes INFRA_FAILURE rows for legs whose ai-job-summary artifact is missing. **Must be passed together with `run-result`.** |
| `run-result` | no | `""` | Aggregate matrix-job result (`needs.<matrix-job>.result`). Accepts `success`, `failure`, `cancelled`, `skipped`. Suppresses synthesis on `cancelled`/`skipped`. **Must be passed together with `expected-jobs`.** |
| `slack-bot-token` | no | `""` | Slack bot token. Both Slack inputs must be set to send. |
| `slack-channel-id` | no | `""` | Slack channel ID. |
| `slack-on-branches` | no | `main` | Comma-separated branches; Slack only sends when `github.ref` matches `refs/heads/<one of these>`. Set empty to always send. |

## Config schema

The action takes inline JSON — no separate config file.

```json
{
  "model": "anthropic/claude-sonnet-4-5-20250929",
  "workspace": "$GITHUB_WORKSPACE",
  "input_dir": "ai_job_summaries",
  "output_dir": "ai_run_summaries"
}
```

- `model` — LLM for narrative generation (or `"none"` to skip narrative).
- `workspace` — base directory for relative paths. Use `$GITHUB_WORKSPACE`
  (the env var; the `${{ github.workspace }}` expression renders the host
  path, which doesn't exist inside container jobs). `$VAR` / `${VAR}` are
  expanded only in `workspace` — `input_dir` / `output_dir` are
  project-relative.
- `input_dir` — directory where per-job artifacts are downloaded.
  Artifacts matching `ai_job_summary_*` are pulled here. **The directory
  is deleted at the end of the action**; do not store anything else here.
- `output_dir` — directory where the aggregated report is written.

## Outputs

| Name | Description |
|------|-------------|
| `report-file` | Path to the aggregated `.md` report |
