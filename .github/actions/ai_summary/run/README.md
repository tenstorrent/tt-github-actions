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
      continue-on-error: true
      uses: tenstorrent/tt-github-actions/.github/actions/ai_summary/run@main
      with:
        config: |
          {
            "model": "${{ vars.AI_SUMMARY_MODEL }}",
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
        # Optional: show linked commit SHAs in the report header.
        commits: |
          [
            {"repo": "tenstorrent/tt-metal", "commit": "${{ needs.resolve-shas.outputs.tt-metal-sha }}"},
            {"repo": "tenstorrent/tt-inference-server", "commit": "${{ needs.resolve-shas.outputs.inference-server-sha }}"}
          ]
```

## Inputs

| Name | Required | Default | Description |
|------|---|---|-------------|
| `config` | yes | — | JSON config string. Keys are listed under [Config schema](#config-schema). |
| `api-key` | yes | — | LLM API key. Pass empty when using `"model": "none"` in config to skip the LLM. |
| `api-url` | yes | — | LLM API URL. Pass empty when using `"model": "none"` in config to skip the LLM. |
| `expected-jobs` | no | `""` | JSON array of expected matrix legs (typically `needs.<matrix-job>.outputs.matrix`). When set with `run-result`, the action synthesizes INFRA_FAILURE rows for legs whose ai-job-summary artifact is missing. **Must be passed together with `run-result`.** |
| `run-result` | no | `""` | Aggregate matrix-job result (`needs.<matrix-job>.result`). Accepts `success`, `failure`, `cancelled`, `skipped`. Suppresses synthesis on `cancelled`/`skipped`. **Must be passed together with `expected-jobs`.** |
| `slack-bot-token` | no | `""` | Slack bot token. Both Slack inputs must be set to send. |
| `slack-channel-id` | no | `""` | Slack channel ID. |
| `slack-on-branches` | no | `main` | Comma-separated branches; Slack only sends when `github.ref` matches `refs/heads/<one of these>`. Set empty to always send. |
| `commits` | no | `""` | JSON array of `{"repo": "owner/name", "commit": "sha"}` objects. Each entry renders as a linked short SHA in the report header. Omit to skip. |

## Config schema

The action takes inline JSON — no separate config file.

```json
{
  "model": "${{ vars.AI_SUMMARY_MODEL }}",
  "workspace": "$GITHUB_WORKSPACE",
  "input_dir": "ai_job_summaries",
  "output_dir": "ai_run_summaries"
}
```

| Key | Required | Description |
|-----|----------|-------------|
| `model` | yes | LLM for the narrative; `"none"` skips it (pass an empty `api-key` too). |
| `workspace` | yes | Base for relative paths. Use the `$GITHUB_WORKSPACE` env var — `${{ github.workspace }}` is the host path, which container jobs don't have. `$VAR` is expanded here only. |
| `input_dir` | yes | Where per-job artifacts are downloaded. **Deleted at the end of the action** — store nothing else there. |
| `output_dir` | yes | Where the report is written. |
| `scope` | no | Set when the calling workflow runs more than once per run, so this report covers only its own legs. Must equal the `scope` given to `ai_summary/job`. |

Any other key passes through untouched.

### Report files

`output_dir` receives `.md`, `.html` and `.json`, stemmed
`ai_run_summary[_<scope>]_r<run>_a<attempt>`. The `.json` carries the per-job
facts without the narrative and is the only place INFRA_FAILURE rows for legs
that produced no artifact appear.

Several attempts coexist on one run — a partial re-run deletes nothing and a
full re-run does not reliably purge — so **consumers must match the
`ai_run_summary…_r<run_id>` prefix and take the highest attempt** rather than
download an exact name.

## Outputs

| Name | Description |
|------|-------------|
| `report-file` | Path to the aggregated `.md` report (the `.json` and `.html` siblings sit next to it) |
