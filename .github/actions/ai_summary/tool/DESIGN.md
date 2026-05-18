# AI Summary Tool — design

For overview, pipeline, and status model see
[`../README.md`](../README.md). For consumer config and inputs see
[`../job/README.md`](../job/README.md) and
[`../run/README.md`](../run/README.md). This file is for contributors
and coding agents changing the tool itself.

## Invariants — preserve when editing

If you find yourself needing to break one, that's a red flag. Verify
against the code and tests before changing.

- **Status is set exactly once per job.** `INFRA_FAILURE` and `TIMEOUT`
  are authoritative; the LLM cannot override them. `_LLM_STATUS_MAP`
  intentionally has no entry for either.
- **No `UNKNOWN` status.** Status is one of the values listed in
  Status logic below. Classification ambiguity lives in the
  `category` field (`unknown`, `infra:no_logs`, `infra:timeout`,
  `infra:partial_logs`, `infra:no_artifact`).
- **`_job.name` and `_job.url` come from CLI args only.** Never from
  log content. `_job.name` must match `expected-jobs[*].name` so
  `ai_run_summary` can reconcile INFRA_FAILURE regardless of what the
  log contains.
- **Run/job context comes from workflow inputs and `GITHUB_*` env
  vars, not live `gh` calls.** Fields aren't always populated on the
  still-running workflow, the API adds an auth dependency the tool
  otherwise doesn't have, and it makes the action flaky under rate
  limits. New context = plumb it through the workflow.
- **`tool_dir` is not a config field.** The action wrapper discovers
  the tool path from its own action location. Don't add it back.
- **Green legs skip the LLM.** Extraction found no errors → write
  summary → done. Don't add LLM calls on the success path; cost would
  scale with passing tests.
- **Pre-LLM SIGALRM (10 min) + OpenAI SDK timeout (5 min) are
  load-bearing.** Together they bound each leg at ~15 min. Callers
  wire the action as `continue-on-error` and assume it cannot hang.
- **Bundled defaults vs project overlay.** Patterns / categories /
  layers ship in `ai_job_summary/config/analysis.yaml`. Projects
  override via their own overlay — don't push consumer-specific
  patterns into the bundled file.
- **Each consumer picks its own model.** `model` is per-project;
  `"none"` skips the LLM. Don't hard-code a model anywhere in the
  tool.

## Architecture

```
ai_job_summary/
  cli.py             → per-job orchestration
  extract.py         → pattern matching, get_job_status(), apply_llm_status()
  summarize.py       → LLM prompt, response parsing, markdown
  context.py         → CI context (PR info, stack-trace snippets)
  config.py          → bundled defaults + project-overlay merge
  config_context.py  → config extraction from log (for error attribution)
  extract_configs.py → layer-aware config extraction
  config/analysis.yaml → bundled: categories, layers, patterns

ai_run_summary/
  cli.py             → run-level orchestration + synthesize_missing_legs()
  parse.py           → reads per-job JSON artifacts
  aggregate.py       → roll up parsed jobs into RunStats
  format.py          → markdown / HTML report
  narrative.py       → optional LLM narrative
  models.py          → status / category data classes + resolve_status()

common/llm_client.py → LLM client (TT Chat / OpenAI compatible)
```

## Per-job flow

1. Parse `--config`; resolve `input_dirs` against `workspace`.
2. **All dirs missing** → INFRA_FAILURE / `infra:no_logs`. No LLM.
3. Extract; run `get_job_status()`.
4. **Pre-LLM timeout** (>10 min) → INFRA_FAILURE / `infra:timeout`. No LLM.
5. **All dirs + SUCCESS** → write summary. No LLM.
6. **Partial dirs + no errors** → INFRA_FAILURE / `infra:partial_logs`. No LLM.
7. **Partial dirs + errors** → call LLM for root cause; status stays INFRA_FAILURE.
8. **All dirs + errors** → call LLM; LLM may refine status via
   `apply_llm_status()`. LLM-reported `infra:*` → INFRA_FAILURE.
9. Write `ai_job_summary_{JOB_ID}.{md,json}`.

## Status logic

Each status has exactly one owner:

- `INFRA_FAILURE` — `ai_job_summary` CLI when `input_dirs`
  partial/missing, or `ai_run_summary` for expected-but-missing legs.
  Never overridden.
- `TIMEOUT` — `get_job_status()` from extraction. Never overridden.
- `SUCCESS` — `get_job_status()` when extraction sees no errors. LLM skipped.
- `FAILED` — `get_job_status()` fallback when exit code is non-zero
  but no crash signal, no timeout, and no parsed test failure. LLM may
  refine.
- `CRASHED` / `TESTS_FAILED` / `EVALS_BELOW_TARGET` — `get_job_status()`
  from extraction; LLM may override via `apply_llm_status()`.

LLM enum → display (`_LLM_STATUS_MAP`): `CRASH` → CRASHED,
`TESTS_FAILED` → TESTS FAILED, `EVALS_BELOW_TARGET` → EVALS BELOW
TARGET, `SUCCESS` → SUCCESS. `TIMEOUT` and `INFRA_FAILURE` are
intentionally absent.

`ai_run_summary.resolve_status()` canonicalises display labels
(`"TESTS FAILED (3 failed)"` → `"TESTS_FAILED"`) for grouping.

## Run-level synthesis

With `--expected-jobs` (matrix output JSON) and `--run-result` (matrix
job's `needs.<>.result`), `ai_run_summary` synthesises `INFRA_FAILURE`
stubs for expected legs with no artifact (runner died, container
setup failed, runner never picked up the job). Suppressed when
`run-result` is `cancelled` or `skipped`.

## Testing

Fixtures: `tests/fixtures/log_samples/` (real CI logs per category),
`tests/fixtures/mock_responses/` (representative LLM JSON).

- `test_extraction.py` — `extract_log()` signals
- `test_status.py` — `get_job_status()` and `apply_llm_status()`
- `test_summarize.py` — prompt, response parsing, markdown
- `test_cli.py` — full pipeline

New failure category = fixture log + mock response + extraction + CLI
tests.
