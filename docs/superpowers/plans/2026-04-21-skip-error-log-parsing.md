# `skip_error_log_parsing` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `skip_error_log_parsing` input to the `collect_data` composite action so callers (notably tt-shield) can skip the per-line log scan that produces `failure_signature` / `failure_description` for failed jobs.

**Architecture:** One boolean flag threaded from `action.yml` input → `generate_data.py` argparse → `create_pipeline_json` → `create_cicd_json_for_data_analysis` → `get_job_rows_from_github_info` → `get_job_row_from_github_job`, which gates the two failure-metadata calls inside a single `if` guard. Default `False` at every layer preserves byte-for-byte behavior for existing callers.

**Tech Stack:** Python 3.10, pytest + `monkeypatch`, GitHub Actions composite (bash + Python).

**Spec:** [`docs/superpowers/specs/2026-04-21-skip-error-log-parsing-design.md`](../specs/2026-04-21-skip-error-log-parsing-design.md)

**Branch:** `acvejic/add-option-to-skip-errors-parsing` (already created, spec already committed).

---

## Files Overview

Created:
- `.github/actions/collect_data/test/test_utils.py` — unit tests for the leaf gate (Task 1).

Modified:
- `.github/actions/collect_data/src/utils.py` — add param to two functions, add gate (Task 1, Task 2).
- `.github/actions/collect_data/src/cicd.py` — add param, forward it (Task 3).
- `.github/actions/collect_data/src/generate_data.py` — add param, argparse flag, forward it (Task 4).
- `.github/actions/collect_data/test/test_generate_data.py` — end-to-end smoke test (Task 5).
- `.github/actions/collect_data/action.yml` — new input + conditional CLI arg (Task 6).
- `.github/actions/collect_data/Readme.md` — document the new input (Task 7).

Order is TDD-first (unit test for leaf → outward plumbing → end-to-end → action surface → docs). Each task ends with a commit.

---

## Task 1: Unit tests + gate at `get_job_row_from_github_job`

Write failing unit tests that expect `get_job_row_from_github_job` to accept a `skip_error_log_parsing` kwarg, then add the parameter and the gate to make them pass.

**Files:**
- Create: `.github/actions/collect_data/test/test_utils.py`
- Modify: `.github/actions/collect_data/src/utils.py:405` (function signature) and `.github/actions/collect_data/src/utils.py:484` (the `if job_status == "failure":` block)

- [ ] **Step 1: Write `test_utils.py` with three unit tests**

Create `.github/actions/collect_data/test/test_utils.py`:

```python
# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from utils import get_job_row_from_github_job


@pytest.fixture
def failed_job():
    return {
        "id": 12345,
        "runner_name": "E150-runner-1",
        "labels": [],
        "name": "test-job",
        "status": "completed",
        "conclusion": "failure",
        "created_at": "2026-04-21T00:00:00Z",
        "started_at": "2026-04-21T00:00:00Z",
        "completed_at": "2026-04-21T00:01:00Z",
        "html_url": "https://github.com/tenstorrent/tt-shield/actions/runs/1/job/12345",
        "steps": [
            {
                "name": "Failing step",
                "status": "completed",
                "conclusion": "failure",
                "number": 1,
                "started_at": "2026-04-21T00:00:00Z",
                "completed_at": "2026-04-21T00:01:00Z",
            }
        ],
    }


@pytest.fixture
def success_job(failed_job):
    job = dict(failed_job)
    job["conclusion"] = "success"
    job["steps"] = [
        {
            "name": "Success step",
            "status": "completed",
            "conclusion": "success",
            "number": 1,
            "started_at": "2026-04-21T00:00:00Z",
            "completed_at": "2026-04-21T00:01:00Z",
        }
    ]
    return job


@pytest.fixture
def fake_logs():
    # 29-char timestamp prefix matches what extract_error_lines_from_logs strips.
    return (
        "2026-04-21T00:00:00.0000000Z ##[group]Setup\n"
        "2026-04-21T00:00:00.0000000Z setting up\n"
        "2026-04-21T00:00:00.0000000Z ##[endgroup]\n"
        "2026-04-21T00:00:01.0000000Z ##[error]Something exploded\n"
        "2026-04-21T00:00:02.0000000Z RuntimeError: boom\n"
    )


def test_skip_error_log_parsing_on(monkeypatch, failed_job, fake_logs):
    monkeypatch.setattr("utils.get_job_logs", lambda repo, job_id: fake_logs)

    row = get_job_row_from_github_job(failed_job, skip_error_log_parsing=True)

    assert row is not None
    assert row["failure_signature"] is None
    assert row["failure_description"] is None


def test_skip_error_log_parsing_off(monkeypatch, failed_job, fake_logs):
    monkeypatch.setattr("utils.get_job_logs", lambda repo, job_id: fake_logs)

    row = get_job_row_from_github_job(failed_job, skip_error_log_parsing=False)

    assert row is not None
    assert row["failure_signature"] == "Failing step"
    assert row["failure_description"] is not None
    assert (
        "Something exploded" in row["failure_description"]
        or "RuntimeError" in row["failure_description"]
    )


def test_skip_flag_noop_for_successful_job(monkeypatch, success_job, fake_logs):
    monkeypatch.setattr("utils.get_job_logs", lambda repo, job_id: fake_logs)

    row = get_job_row_from_github_job(success_job, skip_error_log_parsing=True)

    assert row is not None
    assert row["failure_signature"] is None
    assert row["failure_description"] is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run from `.github/actions/collect_data/`:

```bash
cd .github/actions/collect_data && pytest test/test_utils.py -v
```

Expected: **FAIL**. All three tests error out with `TypeError: get_job_row_from_github_job() got an unexpected keyword argument 'skip_error_log_parsing'`.

- [ ] **Step 3: Add `skip_error_log_parsing` parameter and gate in `utils.py`**

In `.github/actions/collect_data/src/utils.py`, change the function signature at line 405:

From:
```python
def get_job_row_from_github_job(github_job: Dict[str, Any]) -> Dict[str, Any]:
```

To:
```python
def get_job_row_from_github_job(
    github_job: Dict[str, Any], skip_error_log_parsing: bool = False
) -> Dict[str, Any]:
```

Then change the failure-metadata block at lines 482–486:

From:
```python
    failure_signature = None
    failure_description = None
    if job_status == "failure":
        failure_signature = get_job_failure_signature(github_job, logs)
        failure_description = get_failure_description(github_job, logs)
```

To:
```python
    failure_signature = None
    failure_description = None
    if job_status == "failure" and not skip_error_log_parsing:
        failure_signature = get_job_failure_signature(github_job, logs)
        failure_description = get_failure_description(github_job, logs)
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
cd .github/actions/collect_data && pytest test/test_utils.py -v
```

Expected: **PASS**. All three tests green.

- [ ] **Step 5: Run the full existing test suite to confirm no regression**

```bash
cd .github/actions/collect_data && pytest -v
```

Expected: **PASS**. Every existing test still green (default-`False` parameter preserves all call sites).

- [ ] **Step 6: Commit**

```bash
git add .github/actions/collect_data/test/test_utils.py .github/actions/collect_data/src/utils.py
git commit -m "Gate failure-metadata calls behind skip_error_log_parsing flag"
```

---

## Task 2: Thread the flag through `get_job_rows_from_github_info`

`get_job_rows_from_github_info` uses `map(get_job_row_from_github_job, ...)` which cannot forward a kwarg. Rewrite it as a comprehension so the flag can pass through.

**Files:**
- Modify: `.github/actions/collect_data/src/utils.py:528-531`

- [ ] **Step 1: Update `get_job_rows_from_github_info` signature and body**

In `.github/actions/collect_data/src/utils.py`, replace the function at lines 528–531:

From:
```python
def get_job_rows_from_github_info(
    github_pipeline_json: Dict[str, Any], github_jobs_json: Dict[str, Any]
) -> List[Dict[str, Any]]:
    return [row for row in map(get_job_row_from_github_job, github_jobs_json.get("jobs")) if row is not None]
```

To:
```python
def get_job_rows_from_github_info(
    github_pipeline_json: Dict[str, Any],
    github_jobs_json: Dict[str, Any],
    skip_error_log_parsing: bool = False,
) -> List[Dict[str, Any]]:
    rows = [
        get_job_row_from_github_job(job, skip_error_log_parsing=skip_error_log_parsing)
        for job in github_jobs_json.get("jobs")
    ]
    return [row for row in rows if row is not None]
```

- [ ] **Step 2: Run existing tests to verify no regression**

```bash
cd .github/actions/collect_data && pytest -v
```

Expected: **PASS**. All existing tests still green (the new param has a default).

- [ ] **Step 3: Commit**

```bash
git add .github/actions/collect_data/src/utils.py
git commit -m "Thread skip_error_log_parsing through get_job_rows_from_github_info"
```

---

## Task 3: Thread the flag through `create_cicd_json_for_data_analysis`

**Files:**
- Modify: `.github/actions/collect_data/src/cicd.py:33-50`

- [ ] **Step 1: Update `create_cicd_json_for_data_analysis` signature and the forward call**

In `.github/actions/collect_data/src/cicd.py`, replace the function signature at line 33:

From:
```python
def create_cicd_json_for_data_analysis(
    workflow_outputs_dir,
    github_runner_environment,
    github_pipeline_json_filename,
    github_jobs_json_filename,
):
```

To:
```python
def create_cicd_json_for_data_analysis(
    workflow_outputs_dir,
    github_runner_environment,
    github_pipeline_json_filename,
    github_jobs_json_filename,
    skip_error_log_parsing: bool = False,
):
```

Then at line 50, change:

From:
```python
    raw_jobs = get_job_rows_from_github_info(github_pipeline_json, github_jobs_json)
```

To:
```python
    raw_jobs = get_job_rows_from_github_info(
        github_pipeline_json,
        github_jobs_json,
        skip_error_log_parsing=skip_error_log_parsing,
    )
```

- [ ] **Step 2: Run existing tests to verify no regression**

```bash
cd .github/actions/collect_data && pytest -v
```

Expected: **PASS**.

- [ ] **Step 3: Commit**

```bash
git add .github/actions/collect_data/src/cicd.py
git commit -m "Thread skip_error_log_parsing through create_cicd_json_for_data_analysis"
```

---

## Task 4: Thread the flag through `create_pipeline_json` + argparse

**Files:**
- Modify: `.github/actions/collect_data/src/generate_data.py:16-32` (function) and `:68-89` (`__main__` block)

- [ ] **Step 1: Update `create_pipeline_json` signature and the forward call**

In `.github/actions/collect_data/src/generate_data.py`, replace the function at lines 16–24:

From:
```python
def create_pipeline_json(workflow_filename: str, jobs_filename: str, workflow_outputs_dir):

    github_runner_environment = get_github_runner_environment()
    pipeline = create_cicd_json_for_data_analysis(
        workflow_outputs_dir,
        github_runner_environment,
        workflow_filename,
        jobs_filename,
    )
```

To:
```python
def create_pipeline_json(
    workflow_filename: str,
    jobs_filename: str,
    workflow_outputs_dir,
    skip_error_log_parsing: bool = False,
):

    github_runner_environment = get_github_runner_environment()
    pipeline = create_cicd_json_for_data_analysis(
        workflow_outputs_dir,
        github_runner_environment,
        workflow_filename,
        jobs_filename,
        skip_error_log_parsing=skip_error_log_parsing,
    )
```

- [ ] **Step 2: Add argparse flag and pass it through in `__main__`**

In the same file, update the argparse block and the `create_pipeline_json` call in `__main__`.

Replace lines 73–89:

From:
```python
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_id", type=str, required=True, help="Run ID of the workflow")
    parser.add_argument(
        "--output_dir",
        type=str,
        required=False,
        default="generated/cicd",
        help="Output directory for the pipeline json",
    )
    args = parser.parse_args()

    logger.info(f"Creating pipeline JSON for workflow run ID {args.run_id}")
    pipeline, _ = create_pipeline_json(
        workflow_filename=f"{args.output_dir}/{args.run_id}/workflow.json",
        jobs_filename=f"{args.output_dir}/{args.run_id}/workflow_jobs.json",
        workflow_outputs_dir=args.output_dir,
    )
```

To:
```python
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_id", type=str, required=True, help="Run ID of the workflow")
    parser.add_argument(
        "--output_dir",
        type=str,
        required=False,
        default="generated/cicd",
        help="Output directory for the pipeline json",
    )
    parser.add_argument(
        "--skip-error-log-parsing",
        action="store_true",
        help=(
            "Skip per-line log scan that produces failure_signature and "
            "failure_description for failed jobs. Useful for projects whose "
            "logs legitimately contain many 'error'-like lines."
        ),
    )
    args = parser.parse_args()

    logger.info(f"Creating pipeline JSON for workflow run ID {args.run_id}")
    pipeline, _ = create_pipeline_json(
        workflow_filename=f"{args.output_dir}/{args.run_id}/workflow.json",
        jobs_filename=f"{args.output_dir}/{args.run_id}/workflow_jobs.json",
        workflow_outputs_dir=args.output_dir,
        skip_error_log_parsing=args.skip_error_log_parsing,
    )
```

Note: argparse converts `--skip-error-log-parsing` to the attribute `skip_error_log_parsing` automatically (hyphens → underscores).

- [ ] **Step 3: Sanity-check the CLI flag from a shell**

```bash
cd .github/actions/collect_data && python3 src/generate_data.py --help
```

Expected output includes:
```
  --skip-error-log-parsing
                        Skip per-line log scan that produces failure_signature
                        and failure_description for failed jobs. ...
```

- [ ] **Step 4: Run existing tests to verify no regression**

```bash
cd .github/actions/collect_data && pytest -v
```

Expected: **PASS**.

- [ ] **Step 5: Commit**

```bash
git add .github/actions/collect_data/src/generate_data.py
git commit -m "Expose --skip-error-log-parsing CLI flag in generate_data.py"
```

---

## Task 5: End-to-end smoke test

Add a parametrize case to `test_generate_data.py` that exercises the full path with `skip_error_log_parsing=True` and asserts every job's failure metadata is `None`. This proves the plumbing works end-to-end.

**Files:**
- Modify: `.github/actions/collect_data/test/test_generate_data.py` (add a new test function near the existing `test_create_pipeline_json`)

- [ ] **Step 1: Add the smoke test**

Append to `.github/actions/collect_data/test/test_generate_data.py` after the existing `test_create_pipeline_json` function:

```python
@pytest.mark.parametrize(
    "run_id",
    [
        "11236784732",
    ],
)
def test_create_pipeline_json_with_skip_error_log_parsing(run_id):
    """
    End-to-end smoke test for the skip_error_log_parsing flag.
    Every job in the resulting pipeline must have both failure fields as None.
    """
    os.environ["GITHUB_EVENT_NAME"] = "test"

    pipeline, filename = create_pipeline_json(
        workflow_filename=f"test/data/{run_id}/workflow.json",
        jobs_filename=f"test/data/{run_id}/workflow_jobs.json",
        workflow_outputs_dir="test/data",
        skip_error_log_parsing=True,
    )

    assert os.path.exists(filename)

    with open(filename, "r") as file:
        pipeline_json = json.load(file)
        for job in pipeline_json["jobs"]:
            assert job["failure_signature"] is None
            assert job["failure_description"] is None
```

- [ ] **Step 2: Run the new test to verify it passes**

```bash
cd .github/actions/collect_data && pytest test/test_generate_data.py::test_create_pipeline_json_with_skip_error_log_parsing -v
```

Expected: **PASS**.

- [ ] **Step 3: Run the full test suite to confirm everything still works**

```bash
cd .github/actions/collect_data && pytest -v
```

Expected: **PASS**. All tests green.

- [ ] **Step 4: Commit**

```bash
git add .github/actions/collect_data/test/test_generate_data.py
git commit -m "Add end-to-end smoke test for skip_error_log_parsing flag"
```

---

## Task 6: Expose the flag on the composite action

Add `skip_error_log_parsing` as a GitHub Actions input and wire it to the CLI invocation with a bash conditional.

**Files:**
- Modify: `.github/actions/collect_data/action.yml` (input list + `Create JSON` step)

- [ ] **Step 1: Add the new input**

In `.github/actions/collect_data/action.yml`, under the `inputs:` block, add a new entry. The recommended location is right after `ssh-private-key` (line 33–35) so it appears last among inputs:

From:
```yaml
  ssh-private-key:
    description: "SSH private key"
    required: false

runs:
```

To:
```yaml
  ssh-private-key:
    description: "SSH private key"
    required: false
  skip_error_log_parsing:
    description: "Set to 'true' to skip the per-line log scan that produces failure_signature and failure_description for failed jobs. Any value other than the literal string 'true' is treated as false."
    required: false
    default: 'false'

runs:
```

- [ ] **Step 2: Update the `Create JSON` step to conditionally pass the CLI flag**

Replace the `Create JSON` step at lines 54–61:

From:
```yaml
  - name: Create JSON
    env:
      PYTHONPATH: ${{ github.workspace }}
    shell: bash
    run: |
      python3 ${GITHUB_ACTION_PATH}/src/generate_data.py --run_id ${{ inputs.run_id }}
      # Workaround: Copy file to avoid GH upload filename limitations
      mkdir -p reports_json && cp *.json* reports_json/ && zip -rq reports_json.zip reports_json || true
```

To:
```yaml
  - name: Create JSON
    env:
      PYTHONPATH: ${{ github.workspace }}
    shell: bash
    run: |
      EXTRA_ARGS=""
      if [ "${{ inputs.skip_error_log_parsing }}" = "true" ]; then
        EXTRA_ARGS="--skip-error-log-parsing"
      fi
      python3 ${GITHUB_ACTION_PATH}/src/generate_data.py --run_id ${{ inputs.run_id }} $EXTRA_ARGS
      # Workaround: Copy file to avoid GH upload filename limitations
      mkdir -p reports_json && cp *.json* reports_json/ && zip -rq reports_json.zip reports_json || true
```

- [ ] **Step 3: Lint-check the YAML syntactically (no dedicated linter in repo; visual + quick parse check)**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/actions/collect_data/action.yml'))" && echo OK
```

Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add .github/actions/collect_data/action.yml
git commit -m "Add skip_error_log_parsing input to collect_data composite action"
```

---

## Task 7: Document the new input

**Files:**
- Modify: `.github/actions/collect_data/Readme.md` (basic-usage example + new inputs table)

- [ ] **Step 1: Update the Readme**

In `.github/actions/collect_data/Readme.md`, append the following section right before the `# Generate Data Analytics File` heading (line 41):

```markdown
## Optional inputs

| Input | Default | Description |
|-------|---------|-------------|
| `skip_error_log_parsing` | `'false'` | Set to `'true'` to skip the per-line log scan that produces `failure_signature` and `failure_description` for failed jobs. Useful when a project's logs legitimately contain many "error"-like lines (e.g. tt-shield), where the scan produces misleading `failure_description` payloads and spams the collection step's stdout. Logs are still fetched — only the per-line scan and its `logger.info` emissions are skipped. |

Example usage with the flag enabled:

```yaml
- name: Collect CI/CD data
  uses: tenstorrent/tt-github-actions/.github/actions/collect_data@main
  with:
    repository: ${{ github.repository }}
    run_id: ${{ github.event.workflow_run.id }}
    run_attempt: ${{ github.event.workflow_run.run_attempt }}
    skip_error_log_parsing: 'true'
```
```

- [ ] **Step 2: Commit**

```bash
git add .github/actions/collect_data/Readme.md
git commit -m "Document skip_error_log_parsing input in collect_data Readme"
```

---

## Final verification

- [ ] **Step 1: Full test suite**

```bash
cd .github/actions/collect_data && pytest -v
```

Expected: **PASS**. All tests green — existing plus the four new tests (three unit + one smoke).

- [ ] **Step 2: Confirm branch log looks coherent**

```bash
git log --oneline origin/main..HEAD
```

Expected: 8 commits total (1 spec + 7 implementation commits) on branch `acvejic/add-option-to-skip-errors-parsing`:

```
<sha> Document skip_error_log_parsing input in collect_data Readme
<sha> Add skip_error_log_parsing input to collect_data composite action
<sha> Add end-to-end smoke test for skip_error_log_parsing flag
<sha> Expose --skip-error-log-parsing CLI flag in generate_data.py
<sha> Thread skip_error_log_parsing through create_cicd_json_for_data_analysis
<sha> Thread skip_error_log_parsing through get_job_rows_from_github_info
<sha> Gate failure-metadata calls behind skip_error_log_parsing flag
<sha> Add spec: skip_error_log_parsing action input
```

- [ ] **Step 3: Confirm working tree clean**

```bash
git status
```

Expected: `nothing to commit, working tree clean`.
