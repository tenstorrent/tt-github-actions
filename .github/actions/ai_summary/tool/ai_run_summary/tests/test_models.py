# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from ai_run_summary.models import (
    STATUS_EMOJI,
    NON_FAILURE_STATUSES,
    resolve_status,
    ParsedJobSummary,
)


def test_resolve_status_exact():
    assert resolve_status("CRASHED") == "CRASHED"
    assert resolve_status("TIMEOUT") == "TIMEOUT"
    assert resolve_status("SUCCESS") == "SUCCESS"


def test_resolve_status_with_suffix():
    assert resolve_status("TESTS FAILED (3 failed)") == "TESTS_FAILED"
    assert resolve_status("FAILED (exit code 1)") == "FAILED"
    assert resolve_status("EVALS BELOW TARGET (2 failed)") == "EVALS_BELOW_TARGET"


def test_resolve_status_infra():
    assert resolve_status("INFRA_FAILURE") == "INFRA_FAILURE"
    assert resolve_status("INFRA FAILURE") == "INFRA_FAILURE"


def test_resolve_status_empty_collapses_to_failed():
    # UNKNOWN is not a valid status. Missing text is treated as FAILED.
    assert resolve_status("") == "FAILED"


def test_resolve_status_unrecognized_collapses_to_failed():
    assert resolve_status("SOMETHING_ELSE") == "FAILED"


def test_unknown_not_in_emoji_map():
    assert "UNKNOWN" not in STATUS_EMOJI


def test_non_failure_statuses_is_subset_of_emoji():
    for s in NON_FAILURE_STATUSES:
        assert s in STATUS_EMOJI


def test_parsed_job_summary_defaults():
    s = ParsedJobSummary(source_file=Path("test.json"))
    assert s.status == ""
    assert s.is_your_code is None
    assert s.failed_tests == []
