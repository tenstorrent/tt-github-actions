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
