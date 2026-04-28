# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for status determination and LLM status override logic.
"""

from ai_job_summary.extract import JobStatus, apply_llm_status, get_job_status, ExtractedLog


class TestGetJobStatus:
    """get_job_status determines status from extraction signals."""

    def _log(self, **overrides) -> ExtractedLog:
        log = ExtractedLog()
        log.has_crash = overrides.get("has_crash", False)
        log.has_timeout = overrides.get("has_timeout", False)
        log.exit_code = overrides.get("exit_code", None)
        log.failed_tests = overrides.get("failed_tests", [])
        log.failed_evals = overrides.get("failed_evals", [])
        log.error_sections = overrides.get("error_sections", [])
        return log

    def test_crash_is_red(self):
        s = get_job_status(self._log(has_crash=True))
        assert s.status_text == "CRASHED"
        assert not s.is_success

    def test_timeout_is_red(self):
        s = get_job_status(self._log(has_timeout=True))
        assert s.status_text == "TIMEOUT"
        assert not s.is_success

    def test_nonzero_exit_with_failed_tests(self):
        s = get_job_status(self._log(exit_code=1, failed_tests=["test_a"]))
        assert "TESTS FAILED" in s.status_text
        assert "1 failed" in s.status_text

    def test_nonzero_exit_without_tests(self):
        s = get_job_status(self._log(exit_code=1))
        assert "FAILED" in s.status_text
        assert "exit code" in s.status_text

    def test_failed_evals(self):
        s = get_job_status(self._log(failed_evals=["humaneval", "mbpp"]))
        assert "EVALS BELOW TARGET" in s.status_text
        assert "2 failed" in s.status_text

    def test_clean_log_is_success(self):
        s = get_job_status(self._log())
        assert s.is_success
        assert s.status_text == "SUCCESS"


class TestApplyLlmStatus:
    """LLM status overrides extraction, with specific rules."""

    def _status(self, text, code="ORANGE", is_success=False):
        return JobStatus(is_success=is_success, status_code=code, status_text=text)

    # LLM overrides when it disagrees

    def test_crash_overrides_tests_failed(self):
        result = apply_llm_status(self._status("TESTS FAILED (2 failed)"), "CRASH")
        assert result.status_text == "CRASHED"

    def test_tests_failed_overrides_crashed(self):
        result = apply_llm_status(self._status("CRASHED", "RED"), "TESTS_FAILED")
        assert result.status_text == "TESTS FAILED"

    def test_evals_overrides_tests_failed(self):
        result = apply_llm_status(self._status("TESTS FAILED (1 failed)"), "EVALS_BELOW_TARGET")
        assert result.status_text == "EVALS BELOW TARGET"

    def test_success_overrides_failure(self):
        result = apply_llm_status(self._status("TESTS FAILED (1 failed)"), "SUCCESS")
        assert result.is_success

    # Extraction preserved when they agree (keeps enriched labels)

    def test_keeps_test_count_when_agree(self):
        original = self._status("TESTS FAILED (5 failed)")
        result = apply_llm_status(original, "TESTS_FAILED")
        assert result.status_text == "TESTS FAILED (5 failed)"

    def test_keeps_eval_count_when_agree(self):
        original = self._status("EVALS BELOW TARGET (3 failed)", "YELLOW")
        result = apply_llm_status(original, "EVALS_BELOW_TARGET")
        assert result.status_text == "EVALS BELOW TARGET (3 failed)"

    def test_keeps_crashed_when_agree(self):
        original = self._status("CRASHED", "RED")
        result = apply_llm_status(original, "CRASH")
        assert result is original

    # TIMEOUT is never overridden

    def test_timeout_not_overridden_by_success(self):
        original = self._status("TIMEOUT", "RED")
        assert apply_llm_status(original, "SUCCESS") is original

    def test_timeout_not_overridden_by_crash(self):
        original = self._status("TIMEOUT", "RED")
        assert apply_llm_status(original, "CRASH") is original

    def test_timeout_not_overridden_by_tests_failed(self):
        original = self._status("TIMEOUT", "RED")
        assert apply_llm_status(original, "TESTS_FAILED") is original

    # Edge cases

    def test_empty_llm_status_returns_unchanged(self):
        original = self._status("TESTS FAILED (2 failed)")
        assert apply_llm_status(original, "") is original

    def test_unknown_llm_status_returns_unchanged(self):
        original = self._status("TESTS FAILED (2 failed)")
        assert apply_llm_status(original, "MADE_UP") is original
