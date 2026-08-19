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

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `model` | yes | — | LLM used for the narrative. `"none"` skips it; pass an empty `api-key` too. |
| `workspace` | yes | — | Base directory for relative paths. See the note below. |
| `input_dir` | yes | — | Directory the per-job artifacts are downloaded into. **Deleted at the end of the action** — store nothing else there. |
| `output_dir` | yes | — | Directory the aggregated report is written to. |
| `scope` | no | `""` | Discriminator for one invocation of a reusable workflow. Must equal the `scope` given to `ai_summary/job`. See the note below. |

Anything else in the JSON is passed through untouched.

### scope

A workflow invoked more than once per run has one of these jobs per invocation,
and each downloads the whole run's per-job artifacts. Without a scope every
report therefore covers every invocation's legs, and the uploads collide on one
artifact name. Setting it keeps each report to its own legs and gives its
artifact a distinct name.

Matching is on the scope recorded inside each summary (`_job.scope`), not on the
filename, so a scope that doesn't match anything is reported rather than
silently producing a partial report. If the per-leg summaries carry a scope and
this action is given none, it warns and aggregates everything.

### workspace

Use `$GITHUB_WORKSPACE` — the env var. The `${{ github.workspace }}` expression
renders the host path, which doesn't exist inside container jobs. `$VAR` /
`${VAR}` are expanded only in `workspace`; `input_dir` and `output_dir` are
project-relative.

### Report files

`output_dir` receives `.md`, `.html` and `.json`, all stemmed
`ai_run_summary[_<scope>]_r<run>_a<attempt>`. The `.json` carries the factual
per-job data with no LLM narrative, for downstream machine consumers, and is the
only place INFRA_FAILURE rows for legs that produced no artifact appear. The
`.md` and `.json` are both included in the uploaded artifact.

The name carries the run attempt, so **consumers must match the
`ai_run_summary…_r<run_id>` prefix and take the highest attempt** rather than
download an exact name. Several attempts coexist on one run: a partial re-run
deletes nothing, and a full re-run does not reliably purge.

## Outputs

| Name | Description |
|------|-------------|
| `report-file` | Path to the aggregated `.md` report (the `.json` and `.html` siblings sit next to it) |
