# AI Summary Tool

CI log analysis: extract errors → LLM classification → structured markdown
summary. Two stages: `ai_job_summary` per matrix leg, `ai_run_summary` to
aggregate. Each stage has a composite GitHub Action wrapper.

## Architecture

```
ai_job_summary/
  cli.py             → orchestrates the per-job flow
  extract.py         → log extraction, pattern matching, get_job_status(), apply_llm_status()
  summarize.py       → LLM prompt, response parsing, markdown formatting
  context.py         → CI context (PR info, code snippets from stack traces)
  config.py          → bundled-defaults loader + project-overlay merge
  config_context.py  → CI config extraction from log content (for error attribution)
  extract_configs.py → layer-aware config extraction for error attribution
  config/analysis.yaml → bundled defaults: categories, layers, model, patterns

ai_run_summary/
  cli.py             → orchestrates the run-level flow + synthesize_missing_legs()
  parse.py           → reads per-job JSON artifacts produced by ai_job_summary
  aggregate.py       → roll up parsed jobs into RunStats
  format.py          → markdown / HTML report generation
  narrative.py       → optional LLM narrative generation
  models.py          → status / category data classes

common/llm_client.py → LLM API client (TT Chat / OpenAI compatible)
```

## Consumer config (inline JSON, passed via `--config`)

Per-job:
```json
{
  "model": "anthropic/claude-sonnet-4-5-20250929",
  "workspace": "/path/to/repo/checkout",
  "input_dirs": ["generated/test_logs"],
  "output_dir": "generated/ai_summaries"
}
```

Run-level:
```json
{
  "model": "anthropic/claude-sonnet-4-5-20250929",
  "workspace": "/path/to/repo/checkout",
  "input_dir": "ai_job_summaries",
  "output_dir": "ai_run_summaries"
}
```

- `model` — per-project: each consumer picks its own LLM. `"none"` skips
  the LLM call entirely (useful for local dry-runs).
- `workspace` — base directory for relative paths. `Path(workspace) / x`
  resolves `x` against `workspace`; absolute `x` passes through unchanged.
  `$VAR` / `${VAR}` references are expanded against the env so callers
  can write `$GITHUB_WORKSPACE` (the in-container path; the
  `${{ github.workspace }}` expression renders the host path).
- `input_dirs` (job) / `input_dir` (run) — where the tool reads from.
- `output_dir` — where the tool writes to.
- `tool_dir` is **not** a config field. The action discovers the tool path
  automatically from its own location.

Categories / layers / patterns come from the bundled
`ai_job_summary/config/analysis.yaml` and merge with anything supplied in
the project overlay.

## Per-job tool flow

1. Parse `--config` JSON, resolve `input_dirs` against `workspace`.
2. **All dirs missing** → INFRA_FAILURE (`category: infra:no_logs`), no LLM, done.
3. Extract from present dirs, run `get_job_status()`.
4. **Pre-LLM timeout (>10 min)** → INFRA_FAILURE (`category: infra:timeout`), no LLM, done.
5. **All dirs present + SUCCESS** → write summary, no LLM, done.
6. **Partial dirs + no errors** → INFRA_FAILURE (`category: infra:partial_logs`), no LLM, done.
7. **Partial dirs + errors** → call LLM for root cause; status stays INFRA_FAILURE.
8. **All dirs present + errors** → call LLM, LLM may refine status via `apply_llm_status()`.
   If LLM classifies as `infra:*`, status is promoted to INFRA_FAILURE.
9. Write `ai_job_summary_{JOB_ID}.{md,json}` to `output_dir`.

## Status values

Display form (what `status_text` / `_job.status` carry, multi-word values
have spaces):

`SUCCESS` 🟢 · `CRASHED` 🔴 · `TIMEOUT` 🔴 · `TESTS FAILED` 🟠 · `FAILED` 🔴 · `EVALS BELOW TARGET` 🟡 · `INFRA FAILURE` 🟣

`TESTS FAILED` and `FAILED` may carry a suffix in the label
(e.g. `"TESTS FAILED (3 failed)"`). Downstream (`ai_run_summary`) uses
`resolve_status()` to canonicalise to underscored keys
(`TESTS_FAILED`, `INFRA_FAILURE`) for grouping.

LLM enum keys (what the LLM returns in `status` of its response, distinct
from the display form): `CRASH`, `TESTS_FAILED`, `EVALS_BELOW_TARGET`,
`SUCCESS`. `_LLM_STATUS_MAP` translates these to the display `JobStatus`.

Note: there is no `UNKNOWN` *status* — that axis is always one of the
above. Classification ambiguity lives in the separate **category** field
(`unknown` when the LLM cannot pin a root cause; `infra:no_logs`,
`infra:partial_logs`, `infra:timeout`, `infra:no_artifact` for the
infrastructure-failure subtypes).

## Status logic

Status is set exactly once.

- `INFRA_FAILURE` — set by `ai_job_summary` CLI when any `input_dirs` entry is missing,
  or by `ai_run_summary` for expected-but-missing legs. Never overridden by LLM.
- `TIMEOUT` — set by `get_job_status()` from extraction. LLM cannot override.
- `CRASHED` / `TESTS_FAILED` / `EVALS_BELOW_TARGET` — LLM may override
  extraction via `apply_llm_status()`.
- `SUCCESS` — extraction found no errors. LLM is skipped entirely.
- `FAILED` — fallback when extraction sees an error signal but no specific
  category matches; LLM may refine.

`_LLM_STATUS_MAP` maps LLM enums → display `JobStatus`. `TIMEOUT` and
`INFRA_FAILURE` are intentionally absent — set before LLM and never changed.

## JSON output

`_job.name` and `_job.url` always come from CLI args (`--job-name`, `--job-url`),
not from log content. This guarantees the JSON is usable by `ai_run_summary`
regardless of whether the log contains job metadata. `_job.name` MUST match
the corresponding entry in `expected-jobs` (passed to `ai_run_summary`) for
INFRA_FAILURE reconciliation to work.

## Run-level flow

`ai_run_summary` downloads all `ai_job_summary_*` artifacts, parses them,
aggregates by category and status, and writes a markdown + HTML run report.

When called with `--expected-jobs` (the matrix output JSON) and `--run-result`
(the matrix job's `needs.<>.result`), it synthesises `INFRA_FAILURE` stubs
for expected legs that produced no artifact (runner died, container setup
failed, runner never picked up the job). Suppressed when `run-result` is
`cancelled` or `skipped`.

## Action wrappers

### ai_summary/job (`.github/actions/ai_summary/job/action.yml`)

**Runtime:** installs into an active `$VIRTUAL_ENV` (typical for Docker
container jobs with `/opt/venv`); falls back to an ephemeral
`/tmp/ai-summary/venv` on bare runners.

**Inputs:** `config` (required, JSON string), `api-key` (required), `api-url`
(required), `job-name` (optional, defaults to `job.name`; must match
`expected-jobs[*].name` for reconciliation).

**Outputs:** `summary-dir`.

### ai_summary/run (`.github/actions/ai_summary/run/action.yml`)

Runs on `ubuntu-latest`; creates its own venv in `/tmp/ai-summary/venv`
since bare runners have no pre-set `VIRTUAL_ENV`.

**Inputs:** `config` (required, JSON string), `api-key` (required), `api-url`
(required), `expected-jobs` (optional), `run-result` (optional),
`slack-bot-token` / `slack-channel-id` (optional). `expected-jobs` and
`run-result` must be passed together; passing only one is a hard error.

**Outputs:** `report-file`.

## Testing

Fixture-based tests using real CI logs:

- `tests/fixtures/log_samples/` — real log files, one per failure category.
- `tests/fixtures/mock_responses/` — representative LLM JSON responses.

Test files:
- `test_extraction.py` — `extract_log()` on fixture logs; asserts extraction signals.
- `test_status.py` — `get_job_status()` and `apply_llm_status()`.
- `test_summarize.py` — prompt building, response parsing, markdown formatting.
- `test_cli.py` — full pipeline: config → extract → output.

When adding a new failure category: add a fixture log + mock response, then
extraction and CLI tests.

## Portability

To add this tool to a new repo, inline the config JSON in the workflow:

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
    job-name: ${{ matrix.test-group.name }}
```

```yaml
ai-run-summary:
  needs: [generate-matrix, your-matrix-job]
  if: always()
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: tenstorrent/tt-github-actions/.github/actions/ai_summary/run@main
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
        expected-jobs: ${{ needs.generate-matrix.outputs.matrix }}
        run-result:    ${{ needs.your-matrix-job.result }}
```

When the repo checkout is in a subdirectory (e.g. tt-metal's
`docker-job/` pattern), set `workspace` accordingly:
`"workspace": "$GITHUB_WORKSPACE/docker-job"`.
