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
from ai_job_summary.extract import ExtractedLog, JobStatus, apply_llm_status, extract_log, get_job_status
from ai_job_summary.summarize import FailureSummary, build_prompt, format_summary_markdown
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

    def test_finish_marker_without_trailing_newline_counts(self, tmp_path):
        # finish sentinel as the final bytes, no trailing newline — must match
        log = tmp_path / "test.log"
        log.write_text("\n".join([START] + CLEAN_LINES + [FINISH_FAIL]))
        e = extract_log(log, test_patterns=PATTERNS)
        assert e.log_complete is True
        assert e.exit_code == 2

    def test_github_exit_line_not_overridden_by_marker(self, tmp_path):
        # GHA step exit wins over a wrapped phase's clean finish marker
        lines = [START] + CLEAN_LINES + [FINISH_OK, "Process completed with exit code 1."]
        e = _extract(tmp_path, lines)
        assert e.exit_code == 1

    def test_finish_before_trailing_output_is_incomplete(self, tmp_path):
        # finish token echoed mid-run, then output continues past the tail
        # window before truncation → not complete (guards against false green)
        lines = [START, FINISH_OK] + [f"more output {i}" for i in range(15)]
        e = _extract(tmp_path, lines)
        assert e.log_complete is False

    def test_start_below_head_window_is_untracked(self, tmp_path):
        # start sentinel buried below the head window → not run-with-log tracked
        lines = [f"preamble {i}" for i in range(12)] + [START] + CLEAN_LINES + [FINISH_OK]
        e = _extract(tmp_path, lines)
        assert e.log_complete is None


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
    def _md(self, log_complete, status, incomplete_logs=None, error_message=""):
        e = ExtractedLog()
        e.log_complete = log_complete
        if incomplete_logs:
            e.incomplete_logs = incomplete_logs
        s = FailureSummary()
        if error_message:
            s.error_message = error_message
        return format_summary_markdown(s, CIContext(), status, extracted_log=e)

    def test_pure_timeout_shown_in_header(self):
        md = self._md(False, JobStatus(False, "RED", "TIMEOUT"))
        assert "TIMEOUT" in md

    def test_truncation_tags_header_on_other_failure(self):
        # crash + truncation, no error block → header carries the ⚠️ timeout tag
        md = self._md(False, JobStatus(False, "RED", "CRASHED"))
        assert "⚠️ timeout" in md

    def test_truncation_warns_under_error_message(self):
        md = self._md(False, JobStatus(False, "ORANGE", "TESTS FAILED (3 failed)"), error_message="boom")
        assert "truncated due to GitHub timeout" in md
        assert "boom" in md

    def test_warning_names_incomplete_log(self):
        md = self._md(
            False,
            JobStatus(False, "ORANGE", "TESTS FAILED (1 failed)"),
            incomplete_logs=["benchmark.log"],
            error_message="boom",
        )
        assert "benchmark.log" in md

    def test_no_warning_when_complete(self):
        md = self._md(True, JobStatus(True, "GREEN", "SUCCESS"))
        assert "truncated" not in md
        assert "⚠️ timeout" not in md


class TestLLMOverrideOnTruncation:
    """A truncated log (marker absent) hands the verdict to the LLM, but the
    LLM can never call a truncated run green."""

    def _truncated(self, has_timeout):
        e = ExtractedLog()
        e.log_complete = False
        e.has_timeout = has_timeout
        return e

    def test_llm_failure_overrides_marker_timeout(self):
        s = apply_llm_status(JobStatus(False, "RED", "TIMEOUT"), "CRASH", self._truncated(False))
        assert s.status_text == "CRASHED"

    def test_llm_success_cannot_upgrade_marker_timeout(self):
        s = apply_llm_status(JobStatus(False, "RED", "TIMEOUT"), "SUCCESS", self._truncated(False))
        assert s.status_text == "TIMEOUT"

    def test_pattern_timeout_stays_authoritative(self):
        s = apply_llm_status(JobStatus(False, "RED", "TIMEOUT"), "CRASH", self._truncated(True))
        assert s.status_text == "TIMEOUT"

    def test_prompt_flags_truncation_to_llm(self):
        e = ExtractedLog()
        e.log_complete = False
        p = build_prompt(e, CIContext(), {}, {})
        assert "TRUNCATED LOG" in p

    def test_prompt_no_truncation_note_when_complete(self):
        e = ExtractedLog()
        e.log_complete = True
        p = build_prompt(e, CIContext(), {}, {})
        assert "TRUNCATED LOG" not in p


class TestConfig:
    def test_bundled_finish_default_is_set(self):
        assert "tt-log-finish-line" in load_config()["log_complete_marker"]

    def test_bundled_start_default_is_set(self):
        assert "tt-log-start-line" in load_config()["log_start_marker"]

    def test_project_overlay_can_disable(self):
        cfg = load_config({"log_complete_marker": None, "log_start_marker": None})
        assert cfg["log_complete_marker"] is None
        assert cfg["log_start_marker"] is None
