# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the log-completion marker (log_complete_marker config).

The caller's test wrapper appends '[==tt-log-finish-line==] exit_code=N' as the
final log line. Marker absent => shell was hard-killed (GitHub timeout-minutes)
=> TIMEOUT instead of a false SUCCESS. A crash/failure already in the log wins
over the marker verdict.
"""

from ai_job_summary.config import load_config
from ai_job_summary.extract import ExtractedLog, JobStatus, extract_log, get_job_status
from ai_job_summary.summarize import FailureSummary, format_summary_markdown
from ai_job_summary.context import CIContext

MARKER_REGEX = r"^\[==tt-log-finish-line==\]\s*(?:exit_code=(\d+))?"
PATTERNS = {"log_complete_marker": MARKER_REGEX}

CLEAN_LINES = [
    "2026-06-11T00:42:13 pytest session starts",
    "test_a PASSED",
    "test_b PASSED",
]
CRASH_LINES = [
    "2026-06-11T00:42:13 pytest session starts",
    "test_a PASSED",
    "TT_FATAL: device hung on op dispatch",
]
FINISH_OK = "[==tt-log-finish-line==] exit_code=0"
FINISH_FAIL = "[==tt-log-finish-line==] exit_code=2"


def _extract(tmp_path, lines, test_patterns=PATTERNS):
    log = tmp_path / "test.log"
    log.write_text("\n".join(lines) + "\n")
    return extract_log(log, test_patterns=test_patterns)


class TestMarkerExtraction:
    def test_marker_present_sets_log_complete(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES + [FINISH_OK])
        assert e.log_complete is True

    def test_marker_carries_exit_code(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES + [FINISH_OK])
        assert e.exit_code == 0

    def test_marker_nonzero_exit_code(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES + [FINISH_FAIL])
        assert e.exit_code == 2

    def test_marker_absent_sets_log_complete_false(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES)
        assert e.log_complete is False

    def test_marker_not_configured_leaves_none(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES, test_patterns={})
        assert e.log_complete is None

    def test_bare_token_without_payload_counts(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES + ["[==tt-log-finish-line==]"])
        assert e.log_complete is True
        assert e.exit_code is None

    def test_bundled_default_matches_wrapper_line(self, tmp_path):
        marker = load_config()["log_complete_marker"]
        e = _extract(tmp_path, CLEAN_LINES + [FINISH_FAIL], test_patterns={"log_complete_marker": marker})
        assert e.log_complete is True
        assert e.exit_code == 2

    def test_marker_must_anchor_line_start(self, tmp_path):
        # embedded in other output (e.g. echoed cmd) must not count
        lines = CLEAN_LINES + ["echo [==tt-log-finish-line==] exit_code=0"]
        e = _extract(tmp_path, lines)
        assert e.log_complete is False


class TestStatusWithMarker:
    def test_clean_truncated_log_is_timeout(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES)
        s = get_job_status(e)
        assert s.status_text == "TIMEOUT"
        assert not s.is_success

    def test_clean_complete_log_is_success(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES + [FINISH_OK])
        s = get_job_status(e)
        assert s.status_text == "SUCCESS"

    def test_crash_wins_over_truncation(self, tmp_path):
        e = _extract(tmp_path, CRASH_LINES)
        assert e.log_complete is False
        s = get_job_status(e)
        assert s.status_text == "CRASHED"

    def test_nonzero_marker_exit_code_is_failure(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES + [FINISH_FAIL])
        s = get_job_status(e)
        assert "FAILED" in s.status_text

    def test_legacy_log_without_config_is_success(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES, test_patterns={})
        s = get_job_status(e)
        assert s.status_text == "SUCCESS"


class TestReportNote:
    def _md(self, log_complete, status):
        e = ExtractedLog()
        e.log_complete = log_complete
        return format_summary_markdown(FailureSummary(), CIContext(), status, extracted_log=e)

    def test_timeout_note(self):
        md = self._md(False, JobStatus(False, "RED", "TIMEOUT"))
        assert "completion marker" in md
        assert "timeout-minutes" in md

    def test_independent_issue_note_on_crash(self):
        md = self._md(False, JobStatus(False, "RED", "CRASHED"))
        assert "incomplete" in md
        assert "independent" in md

    def test_no_note_when_complete(self):
        md = self._md(True, JobStatus(True, "GREEN", "SUCCESS"))
        assert "incomplete" not in md
        assert "completion marker" not in md


class TestConfig:
    def test_bundled_default_is_set(self):
        assert "tt-log-finish-line" in load_config()["log_complete_marker"]

    def test_project_overlay_can_disable(self):
        cfg = load_config({"log_complete_marker": None})
        assert cfg["log_complete_marker"] is None
