# Design: `skip_error_log_parsing` action input

**Date:** 2026-04-21
**Status:** Draft — awaiting user review
**Area:** `.github/actions/collect_data`

## Goal

Allow callers of the `collect_data` composite action to skip the log-based
failure-metadata extraction that runs for every failed job.

Motivating case: tt-shield logs legitimately contain many lines matching the
error markers (`##[error]`, `error:`, `exception:`, `failed`). As a result:

- The emitted `failure_description` is misleading (dominated by false-positive
  matches, not the true root cause).
- Each matched line triggers a `logger.info("Error line: ...")` call
  (`utils.py:262`), flooding the action's own stdout during collection.

A new opt-in action input lets tt-shield (and any similar consumer) suppress
this scan while leaving every other caller's behavior identical.

## Scope

### In scope

A new action input `skip_error_log_parsing` (default `'false'`) that, when set
to `'true'`, causes `get_job_row_from_github_job` to skip **both** of these
calls for failed jobs:

- `get_job_failure_signature(...)` — `utils.py:158`. Produces
  `failure_signature` and the optional `[tt-triage]` tag via a substring check
  on the log.
- `get_failure_description(...)` — `utils.py:190`. Produces
  `failure_description` via `extract_error_lines_from_logs`, which scans every
  log line for the error markers above.

When skipped, both fields are emitted as `None` in the pipeline JSON. The
fields are already `Optional[str]` in `pydantic_models.py:109-110`, so no
schema change is required.

### Out of scope

- **Log fetching.** `get_job_logs` still runs because `job_inputs_from_logs`
  and `docker_image_from_logs` need the raw log. The "faster collect" benefit
  comes from skipping the per-line pattern scan and its associated `logger.info`
  spam, not from skipping the HTTP call.
- **Per-project auto-detection.** The flag is set explicitly per caller. We
  considered hard-coding a "forge-only" allowlist; it would make the intent
  implicit, require a code change to onboard a new project, and bury the
  decision far from the workflow that actually runs the action.
- **Existing callers.** `produce_data.yml` and all downstream repos (tt-forge,
  tt-mlir, tt-forge-onnx, tt-xla) are not modified. Their behavior is
  unchanged because the default is `'false'`.

## Architecture & data flow

The flag follows the same path as `run_id` and `repository` today: action
input → Python CLI arg → function parameter, threaded down to the one
function that needs it.

```
action.yml (input: skip_error_log_parsing, default 'false')
        │
        ▼
generate_data.py  --skip-error-log-parsing  (argparse store_true)
        │
        ▼
create_pipeline_json(..., skip_error_log_parsing: bool = False)
        │
        ▼
cicd.py: create_cicd_json_for_data_analysis(..., skip_error_log_parsing=False)
        │
        ▼
utils.py: get_job_rows_from_github_info(..., skip_error_log_parsing=False)
        │
        ▼
utils.py: get_job_row_from_github_job(github_job, skip_error_log_parsing=False)
        │
        ▼
    if job_status == "failure" and not skip_error_log_parsing:
        failure_signature   = get_job_failure_signature(github_job, logs)
        failure_description = get_failure_description(github_job, logs)
    # else: both stay None
```

### Key design choices

- **Default `False` at every layer.** Preserves byte-for-byte behavior for
  every existing caller (including tests that call `create_pipeline_json` or
  `get_job_row_from_github_job` without the flag).
- **One gating site.** The `if` guard lives in exactly one place
  (`get_job_row_from_github_job`). `get_job_failure_signature` and
  `get_failure_description` are not modified — they stay pure and testable on
  their own.
- **Naming.** Action input: `skip_error_log_parsing` (matches existing
  snake_case inputs: `run_id`, `sftp_host`). Argparse flag:
  `--skip-error-log-parsing` (CLI convention is hyphens). Python identifier:
  `skip_error_log_parsing` (PEP 8).
- **Boolean conversion in `action.yml`.** The bash step conditionally appends
  `--skip-error-log-parsing` only when the input equals the literal string
  `"true"`. Anything else (including `"True"`, `"1"`, `"yes"`, empty) is
  treated as false. This keeps the Python side on a clean `store_true` bool.

Illustrative bash:

```bash
EXTRA_ARGS=""
if [ "${{ inputs.skip_error_log_parsing }}" = "true" ]; then
  EXTRA_ARGS="--skip-error-log-parsing"
fi
python3 ${GITHUB_ACTION_PATH}/src/generate_data.py \
    --run_id ${{ inputs.run_id }} $EXTRA_ARGS
```

## Error handling & edge cases

- **`logs is None`** (GitHub CLI fails, job log expired, etc.). Behavior
  unchanged. With flag off, `get_failure_description` swallows the resulting
  `AttributeError` in its existing `try/except` and returns an error string;
  `get_job_failure_signature` short-circuits because `if logs and triage_phrase
  in logs` is False. With flag on, neither function is called — the job emits
  the same `None/None` pair as a successful job would.
- **Invalid action-input value.** Anything other than the literal `"true"` is
  treated as false. This is documented in the input description in `action.yml`.
- **Downstream callers that still want the data.** Any caller that does not
  set the flag continues to receive populated `failure_signature` and
  `failure_description` fields. No pydantic schema change.
- **Direct Python importers.** Any script that imports
  `get_job_row_from_github_job` from `utils` keeps working unchanged — the
  new parameter has a default of `False`.

## Testing

### Existing tests

No edits required. `test_generate_data.py` is the only test that exercises
this code path and it calls `create_pipeline_json(...)` with keyword args
only. Adding a default-`False` parameter preserves every call site.

If any currently-passing test breaks during implementation, the failure will
be investigated and fixed — not silenced.

### New tests

A new file `.github/actions/collect_data/test/test_utils.py` with three unit
tests, using `pytest.monkeypatch` to stub `get_job_logs`:

1. **`test_skip_error_log_parsing_on`** — synthetic failed-job dict,
   `get_job_logs` stubbed to return a log containing `error:` and `##[error]`
   markers. Call `get_job_row_from_github_job(job, skip_error_log_parsing=True)`.
   Assert both `failure_signature` and `failure_description` are `None`.
2. **`test_skip_error_log_parsing_off`** — same fixture, call with
   `skip_error_log_parsing=False`. Assert `failure_description` is non-`None`
   and contains at least one scraped line. Proves flag-off still works.
3. **`test_skip_flag_noop_for_successful_job`** — successful job, flag=True.
   Assert both fields are `None` (same as flag-off for a success). Confirms
   the flag is a no-op when there is no failure to describe.

### Existing end-to-end test

One new parametrize case added to `test_generate_data.py::test_create_pipeline_json`:
pass `skip_error_log_parsing=True` to `create_pipeline_json` against an
existing fixture that contains failed jobs, and assert every
`job["failure_description"]` and `job["failure_signature"]` in the resulting
pipeline JSON is `None`.

### CI

Test runs happen via `.github/workflows/test_collect_data_action.yml`. No new
CI wiring is required — pytest discovers the new file automatically.

## Files changed

| File | Change |
|------|--------|
| `.github/actions/collect_data/action.yml` | Add `skip_error_log_parsing` input (default `'false'`). Modify the "Create JSON" step to conditionally append `--skip-error-log-parsing` to the Python invocation. |
| `.github/actions/collect_data/src/generate_data.py` | Add `--skip-error-log-parsing` argparse `store_true` flag. Add `skip_error_log_parsing: bool = False` param to `create_pipeline_json`, pass into `create_cicd_json_for_data_analysis`. Pass `args.skip_error_log_parsing` from `__main__`. |
| `.github/actions/collect_data/src/cicd.py` | Add `skip_error_log_parsing: bool = False` param to `create_cicd_json_for_data_analysis`, pass into `get_job_rows_from_github_info`. |
| `.github/actions/collect_data/src/utils.py` | Add `skip_error_log_parsing: bool = False` to `get_job_rows_from_github_info` and `get_job_row_from_github_job`. Rewrite `get_job_rows_from_github_info` from `map(...)` to a comprehension so it can forward the kwarg. Add the `and not skip_error_log_parsing` guard around the two failure-metadata calls near `utils.py:484-486`. |
| `.github/actions/collect_data/test/test_utils.py` | **New file.** Three unit tests described above. |
| `.github/actions/collect_data/test/test_generate_data.py` | Add one parameterized case exercising `skip_error_log_parsing=True` end-to-end. |
| `.github/actions/collect_data/Readme.md` | Document the new input, including the tt-shield motivation and an example. |

Total: 5 source/workflow files, 2 test files, 1 doc file. No schema changes,
no new dependencies, no new workflow files.

## Consumer-side note (outside this repo)

For tt-shield to benefit, its workflow that calls `collect_data` must opt in:

```yaml
- uses: tenstorrent/tt-github-actions/.github/actions/collect_data@main
  with:
    repository: ${{ github.repository }}
    run_id: ${{ github.event.workflow_run.id }}
    run_attempt: ${{ github.event.workflow_run.run_attempt }}
    skip_error_log_parsing: 'true'
```

That change lives in the tt-shield repository, not here. It is called out so
nobody is surprised that pulling this PR by itself does not change tt-shield's
collect output.
