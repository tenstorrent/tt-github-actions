# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the run-with-log completion markers (log_start_marker /
log_complete_marker config).

run-with-log writes a start sentinel as a log's first line and a finish sentinel
(carrying the exit code) as its last. Evaluation is per file: a log that started
but never finished was hard-killed (GitHub timeout-minutes) => TIMEOUT instead of
a false SUCCESS. A crash/failure already in the log wins over the truncation. A
log without the start sentinel is not run-with-log tracked (e.g. a backgrounded
server's tail) and is ignored entirely.
"""

from ai_job_summary.config import load_config
from ai_job_summary.extract import ExtractedLog, JobStatus, extract_log, get_job_status
from ai_job_summary.summarize import FailureSummary, format_summary_markdown
from ai_job_summary.context import CIContext

START_REGEX = r"^\[==tt-log-start-line==\]"
FINISH_REGEX = r"^\[==tt-log-finish-line==\]\s*(?:exit_code=(\d+))?"
PATTERNS = {"log_start_marker": START_REGEX, "log_complete_marker": FINISH_REGEX}

START = "[==tt-log-start-line==]"
FINISH_OK = "[==tt-log-finish-line==] exit_code=0"
FINISH_FAIL = "[==tt-log-finish-line==] exit_code=2"

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


def _extract(tmp_path, lines, test_patterns=PATTERNS):
    log = tmp_path / "test.log"
    log.write_text("\n".join(lines) + "\n")
    return extract_log(log, test_patterns=test_patterns)


def _extract_dir(tmp_path, files, test_patterns=PATTERNS):
    """files: {name: lines}. Writes each into a dir, extracts over the dir."""
    d = tmp_path / "logs"
    d.mkdir()
    for name, lines in files.items():
        (d / name).write_text("\n".join(lines) + "\n")
    return extract_log(d, test_patterns=test_patterns)


class TestMarkerExtraction:
    def test_started_and_finished_is_complete(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES + [FINISH_OK])
        assert e.log_complete is True

    def test_marker_carries_exit_code(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES + [FINISH_OK])
        assert e.exit_code == 0

    def test_marker_nonzero_exit_code(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES + [FINISH_FAIL])
        assert e.exit_code == 2

    def test_started_but_unfinished_is_false(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES)
        assert e.log_complete is False
        assert e.incomplete_logs == ["test.log"]

    def test_log_without_start_is_untracked(self, tmp_path):
        # marker configured, but this log was not produced by run-with-log
        e = _extract(tmp_path, CLEAN_LINES)
        assert e.log_complete is None

    def test_marker_not_configured_leaves_none(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES, test_patterns={})
        assert e.log_complete is None

    def test_bare_finish_token_without_payload_counts(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES + ["[==tt-log-finish-line==]"])
        assert e.log_complete is True
        assert e.exit_code is None

    def test_bundled_default_matches_wrapper_lines(self, tmp_path):
        cfg = load_config()
        patterns = {"log_start_marker": cfg["log_start_marker"], "log_complete_marker": cfg["log_complete_marker"]}
        e = _extract(tmp_path, [START] + CLEAN_LINES + [FINISH_FAIL], test_patterns=patterns)
        assert e.log_complete is True
        assert e.exit_code == 2

    def test_finish_must_anchor_line_start(self, tmp_path):
        # an echoed finish token mid-line must not count as completion
        lines = [START] + CLEAN_LINES + ["echo [==tt-log-finish-line==] exit_code=0"]
        e = _extract(tmp_path, lines)
        assert e.log_complete is False


class TestMultipleLogs:
    def test_all_tracked_logs_finished(self, tmp_path):
        e = _extract_dir(
            tmp_path,
            {
                "install.log": [START] + CLEAN_LINES + [FINISH_OK],
                "benchmark.log": [START] + CLEAN_LINES + [FINISH_OK],
            },
        )
        assert e.log_complete is True

    def test_one_tracked_log_truncated(self, tmp_path):
        e = _extract_dir(
            tmp_path,
            {
                "install.log": [START] + CLEAN_LINES + [FINISH_OK],
                "benchmark.log": [START] + CLEAN_LINES,  # killed mid-phase
            },
        )
        assert e.log_complete is False
        assert e.incomplete_logs == ["benchmark.log"]

    def test_untracked_log_is_ignored(self, tmp_path):
        # server tail has no start sentinel; must not drag the verdict to False
        e = _extract_dir(
            tmp_path,
            {
                "benchmark.log": [START] + CLEAN_LINES + [FINISH_OK],
                "vllm_server.log": ["INFO: serving requests", "INFO: still up"],
            },
        )
        assert e.log_complete is True


class TestStatusWithMarker:
    def test_started_truncated_log_is_timeout(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES)
        s = get_job_status(e)
        assert s.status_text == "TIMEOUT"
        assert not s.is_success

    def test_complete_log_is_success(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES + [FINISH_OK])
        s = get_job_status(e)
        assert s.status_text == "SUCCESS"

    def test_untracked_log_is_success(self, tmp_path):
        # non-adopter workflow (no start sentinel) must NOT be false-flagged
        e = _extract(tmp_path, CLEAN_LINES)
        s = get_job_status(e)
        assert s.status_text == "SUCCESS"

    def test_crash_wins_over_truncation(self, tmp_path):
        e = _extract(tmp_path, [START] + CRASH_LINES)
        assert e.log_complete is False
        s = get_job_status(e)
        assert s.status_text == "CRASHED"

    def test_nonzero_marker_exit_code_is_failure(self, tmp_path):
        e = _extract(tmp_path, [START] + CLEAN_LINES + [FINISH_FAIL])
        s = get_job_status(e)
        assert "FAILED" in s.status_text

    def test_legacy_log_without_config_is_success(self, tmp_path):
        e = _extract(tmp_path, CLEAN_LINES, test_patterns={})
        s = get_job_status(e)
        assert s.status_text == "SUCCESS"


class TestReportNote:
    def _md(self, log_complete, status, incomplete_logs=None):
        e = ExtractedLog()
        e.log_complete = log_complete
        if incomplete_logs:
            e.incomplete_logs = incomplete_logs
        return format_summary_markdown(FailureSummary(), CIContext(), status, extracted_log=e)

    def test_timeout_note(self):
        md = self._md(False, JobStatus(False, "RED", "TIMEOUT"))
        assert "completion marker" in md
        assert "timeout-minutes" in md

    def test_independent_issue_note_on_crash(self):
        md = self._md(False, JobStatus(False, "RED", "CRASHED"))
        assert "incomplete" in md
        assert "independent" in md

    def test_note_names_truncated_log(self):
        md = self._md(False, JobStatus(False, "RED", "TIMEOUT"), incomplete_logs=["benchmark.log"])
        assert "benchmark.log" in md

    def test_no_note_when_complete(self):
        md = self._md(True, JobStatus(True, "GREEN", "SUCCESS"))
        assert "incomplete" not in md
        assert "completion marker" not in md


class TestConfig:
    def test_bundled_finish_default_is_set(self):
        assert "tt-log-finish-line" in load_config()["log_complete_marker"]

    def test_bundled_start_default_is_set(self):
        assert "tt-log-start-line" in load_config()["log_start_marker"]

    def test_project_overlay_can_disable(self):
        cfg = load_config({"log_complete_marker": None, "log_start_marker": None})
        assert cfg["log_complete_marker"] is None
        assert cfg["log_start_marker"] is None
