# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from utils import assert_workflow_completed, get_job_row_from_github_job, get_pipeline_row_from_github_info


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
def fake_logs():
    # 29-char timestamp prefix matches what extract_error_lines_from_logs strips.
    return (
        "2026-04-21T00:00:00.0000000Z ##[group]Setup\n"
        "2026-04-21T00:00:00.0000000Z setting up\n"
        "2026-04-21T00:00:00.0000000Z ##[endgroup]\n"
        "2026-04-21T00:00:01.0000000Z ##[error]Something exploded\n"
        "2026-04-21T00:00:02.0000000Z RuntimeError: boom\n"
    )


@pytest.mark.parametrize("skip_flag", [True, False])
def test_skip_error_log_parsing(monkeypatch, failed_job, fake_logs, skip_flag):
    monkeypatch.setattr("utils.get_job_logs", lambda repo, job_id: fake_logs)

    row = get_job_row_from_github_job(failed_job, skip_error_log_parsing=skip_flag)

    assert row is not None
    if skip_flag:
        assert row["failure_signature"] is None
        assert row["failure_description"] is None
    else:
        assert row["failure_signature"] == "Failing step"
        assert "Something exploded" in row["failure_description"]
        assert "RuntimeError" in row["failure_description"]


def test_skip_log_download_avoids_fetch(monkeypatch, failed_job):
    def _fail_if_called(repo, job_id):
        raise AssertionError("get_job_logs must not be called when skip_log_download=True")

    monkeypatch.setattr("utils.get_job_logs", _fail_if_called)

    row = get_job_row_from_github_job(failed_job, skip_log_download=True)

    assert row is not None
    assert row["docker_image"] is None
    assert row["job_matrix_config"] is None
    # Without logs, failure parsing must not run — the fields stay None rather
    # than becoming an exception string from extract_error_lines_from_logs(None).
    assert row["failure_signature"] is None
    assert row["failure_description"] is None


def test_assert_workflow_completed_default_rejects_in_progress():
    with pytest.raises(RuntimeError, match="Collect data should only run on finished workflows"):
        assert_workflow_completed({"id": 99, "status": "in_progress"})


def test_assert_workflow_completed_accepts_completed_and_overrides():
    assert_workflow_completed({"id": 99, "status": "completed"})
    assert_workflow_completed({"id": 99, "status": "in_progress"}, expected_status="in_progress")
    assert_workflow_completed({"id": 99, "status": "in_progress"}, expected_status="completed,in_progress")
    assert_workflow_completed({"id": 99, "status": "queued"}, expected_status="any")
    assert_workflow_completed({"id": 99, "status": "queued"}, expected_status="*")


def test_assert_workflow_completed_non_terminal_mismatch_message():
    with pytest.raises(RuntimeError, match="Expected workflow status in \['in_progress'\], got 'queued'"):
        assert_workflow_completed({"id": 99, "status": "queued"}, expected_status="in_progress")


def test_get_pipeline_row_skips_queued_jobs_with_null_started_at():
    pipeline_json = {
        "id": 1001,
        "created_at": "2026-04-21T00:00:00Z",
        "updated_at": "2026-04-21T00:05:00Z",
        "name": "CI",
        "head_branch": "main",
        "head_sha": "abc1234",
        "html_url": "https://github.com/tenstorrent/tt-github-actions/actions/runs/1001",
        "repository": {
            "name": "tt-github-actions",
            "html_url": "https://github.com/tenstorrent/tt-github-actions",
        },
        "head_commit": {"author": {"name": "tester"}},
    }
    jobs_json = {
        "jobs": [
            {"id": 1, "started_at": None, "status": "queued"},
            {"id": 2, "started_at": "2026-04-21T00:01:00Z", "status": "in_progress"},
        ]
    }
    row = get_pipeline_row_from_github_info(
        {"github_event_name": "workflow_dispatch"},
        pipeline_json,
        jobs_json,
    )
    assert row["github_pipeline_id"] == 1001
    assert row["pipeline_start_ts"].startswith("2026-04-21T00:01:00")
