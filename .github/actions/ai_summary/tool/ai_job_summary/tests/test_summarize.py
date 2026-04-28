# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for summarize.py — prompt building, LLM response parsing,
emoji rendering, and markdown formatting.
"""

import json

import pytest

from ai_job_summary.context import CIContext
from ai_job_summary.extract import ExtractedLog, JobStatus
from ai_job_summary.summarize import (
    FailureSummary,
    _emoji,
    _parse_llm_response,
    _truncate_prompt_if_needed,
    build_prompt,
    format_infra_failure_markdown,
    format_summary_markdown,
)


# ── _emoji ────────────────────────────────────────────────────────────────────


class TestEmoji:
    def test_red(self):
        assert _emoji("RED") == "🔴"

    def test_orange(self):
        assert _emoji("ORANGE") == "🟠"

    def test_yellow(self):
        assert _emoji("YELLOW") == "🟡"

    def test_green(self):
        assert _emoji("GREEN") == "🟢"

    def test_purple(self):
        assert _emoji("PURPLE") == "🟣"

    def test_unknown_code_returns_itself(self):
        assert _emoji("MAGENTA") == "MAGENTA"


# ── _parse_llm_response ──────────────────────────────────────────────────────


class TestParseLlmResponse:
    """Test JSON extraction and field mapping from raw LLM text."""

    def _json(self, **fields) -> str:
        data = {
            "status": "CRASH",
            "category": "app:cli",
            "subcategory": "unrecognized_argument",
            "layer": "application",
            "root_cause": "server failed",
            "error_message": "error: unrecognized arguments",
            "failed_tests": [],
            "suggested_action": "fix the arg",
            "confidence": "high",
            **fields,
        }
        return json.dumps(data)

    def test_parses_plain_json(self):
        s = _parse_llm_response(self._json())
        assert s.status == "CRASH"
        assert s.category == "app:cli"
        assert s.confidence == "high"

    def test_parses_json_in_markdown_code_block(self):
        text = "Here is the analysis:\n```json\n" + self._json() + "\n```\nDone."
        s = _parse_llm_response(text)
        assert s.status == "CRASH"
        assert s.category == "app:cli"

    def test_parses_json_in_bare_code_block(self):
        text = "```\n" + self._json() + "\n```"
        s = _parse_llm_response(text)
        assert s.category == "app:cli"

    def test_invalid_json_sets_root_cause_error(self):
        s = _parse_llm_response("this is not json at all")
        assert "Failed to parse" in s.root_cause
        assert s.error_message.startswith("this is not json")

    def test_missing_fields_use_defaults(self):
        s = _parse_llm_response(json.dumps({"category": "infra:network"}))
        assert s.status == ""
        assert s.subcategory == ""
        assert s.layer == ""
        assert s.confidence == "medium"  # default
        assert s.failed_tests == []
        assert s.is_your_code is None

    def test_all_fields_mapped(self):
        s = _parse_llm_response(
            self._json(
                problematic_layer="serving",
                file="server.py",
                is_your_code=True,
                pr_files_in_stack=["a.py", "b.py"],
                unknown_pattern="NEW_THING",
            )
        )
        assert s.problematic_layer == "serving"
        assert s.file == "server.py"
        assert s.is_your_code is True
        assert s.pr_files_in_stack == ["a.py", "b.py"]
        assert s.unknown_pattern == "NEW_THING"

    def test_unknown_status_ignored(self):
        s = _parse_llm_response(self._json(status="BANANA"))
        assert s.status == ""

    def test_valid_statuses_accepted(self):
        for status in ("CRASH", "TESTS_FAILED", "EVALS_BELOW_TARGET", "SUCCESS"):
            s = _parse_llm_response(self._json(status=status))
            assert s.status == status


# ── _truncate_prompt_if_needed ────────────────────────────────────────────────


class TestTruncatePrompt:
    def test_short_prompt_unchanged(self):
        prompt = "Hello world"
        assert _truncate_prompt_if_needed(prompt, 1000) == prompt

    def test_long_prompt_without_markers_simple_truncation(self):
        prompt = "x" * 200
        result = _truncate_prompt_if_needed(prompt, 100)
        assert len(result) <= 200  # truncated + suffix
        assert "truncated" in result

    def test_preserves_task_section(self):
        """When there's enough room after truncating the log, the task section survives."""
        log_content = "A" * 5000
        task = "Do the thing."
        prompt = f"Intro\n## EXTRACTED LOG\n{log_content}\n## YOUR TASK\n{task}\n"
        # Limit must be large enough for intro + truncated log + task
        result = _truncate_prompt_if_needed(prompt, 3000)
        assert "## YOUR TASK" in result
        assert task in result

    def test_falls_back_to_simple_truncation_when_limit_too_small(self):
        """When the limit is too small for structured truncation, falls back to simple cut."""
        log_content = "A" * 5000
        prompt = f"Intro\n## EXTRACTED LOG\n{log_content}\n## YOUR TASK\nDo the thing.\n"
        result = _truncate_prompt_if_needed(prompt, 500)
        assert "truncated" in result

    def test_exact_limit_not_truncated(self):
        prompt = "x" * 100
        assert _truncate_prompt_if_needed(prompt, 100) == prompt


# ── format_summary_markdown ───────────────────────────────────────────────────


def _make_extracted(**overrides) -> ExtractedLog:
    log = ExtractedLog()
    log.job_name = overrides.get("job_name", "test-job")
    log.job_url = overrides.get("job_url", "")
    log.failed_tests = overrides.get("failed_tests", [])
    log.failed_evals = overrides.get("failed_evals", [])
    log.time_after_crash_seconds = overrides.get("time_after_crash_seconds", None)
    return log


class TestFormatSummaryMarkdown:
    """Test the markdown output for various scenarios."""

    def test_success_header_has_green_emoji(self):
        md = format_summary_markdown(
            FailureSummary(),
            CIContext(),
            JobStatus(True, "GREEN", "SUCCESS"),
        )
        assert "🟢" in md
        assert "SUCCESS" in md

    def test_crash_header_has_red_emoji(self):
        md = format_summary_markdown(
            FailureSummary(category="app:cli", error_message="boom"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
        )
        assert "🔴" in md
        assert "CRASHED" in md

    def test_job_link_in_header(self):
        log = _make_extracted(job_name="my-job", job_url="https://github.com/org/repo/actions/runs/1/job/2")
        md = format_summary_markdown(
            FailureSummary(category="app:cli"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
            extracted_log=log,
        )
        assert "[my-job]" in md
        assert "(https://github.com/org/repo/actions/runs/1/job/2)" in md

    def test_job_url_without_name_uses_view_job(self):
        log = _make_extracted(job_name="", job_url="https://example.com/job")
        md = format_summary_markdown(
            FailureSummary(category="app:cli"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
            extracted_log=log,
        )
        assert "[View Job]" in md

    def test_success_hides_error_details(self):
        md = format_summary_markdown(
            FailureSummary(category="app:cli", error_message="boom", root_cause="bad thing"),
            CIContext(),
            JobStatus(True, "GREEN", "SUCCESS"),
        )
        assert "Error Message" not in md
        assert "Root Cause" not in md
        assert "boom" not in md

    def test_failure_shows_classification(self):
        md = format_summary_markdown(
            FailureSummary(category="tt-metal:fabric", subcategory="timeout", layer="framework"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
        )
        assert "Classification" in md
        assert "tt-metal:fabric" in md
        assert "timeout" in md

    def test_failure_shows_error_message(self):
        md = format_summary_markdown(
            FailureSummary(error_message="RuntimeError: device timeout"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
        )
        assert "RuntimeError: device timeout" in md
        assert "```" in md  # code block

    def test_failure_shows_root_cause(self):
        md = format_summary_markdown(
            FailureSummary(root_cause="Ethernet fabric lost sync"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
        )
        assert "Root Cause" in md
        assert "Ethernet fabric lost sync" in md

    def test_failure_shows_suggested_action(self):
        md = format_summary_markdown(
            FailureSummary(suggested_action="Restart the runner"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
        )
        assert "Suggested Action" in md
        assert "Restart the runner" in md

    def test_failed_tests_shown_in_collapsible(self):
        log = _make_extracted(failed_tests=["test_a", "test_b", "test_c"])
        md = format_summary_markdown(
            FailureSummary(),
            CIContext(),
            JobStatus(False, "ORANGE", "TESTS FAILED"),
            extracted_log=log,
        )
        assert "<details>" in md
        assert "Failed Tests" in md
        assert "`test_a`" in md
        assert "`test_c`" in md

    def test_failed_tests_truncated_at_10(self):
        tests = [f"test_{i}" for i in range(15)]
        log = _make_extracted(failed_tests=tests)
        md = format_summary_markdown(
            FailureSummary(),
            CIContext(),
            JobStatus(False, "ORANGE", "TESTS FAILED"),
            extracted_log=log,
        )
        assert "... and 5 more" in md

    def test_failed_evals_shown_separately(self):
        log = _make_extracted(failed_evals=["humaneval", "mbpp"])
        md = format_summary_markdown(
            FailureSummary(),
            CIContext(),
            JobStatus(False, "YELLOW", "EVALS BELOW TARGET"),
            extracted_log=log,
        )
        assert "Failed Evals" in md
        assert "`humaneval`" in md

    def test_evals_excluded_from_failed_tests(self):
        """If an eval name appears in both failed_tests and failed_evals, don't show it twice."""
        log = _make_extracted(failed_tests=["humaneval", "test_a"], failed_evals=["humaneval"])
        md = format_summary_markdown(
            FailureSummary(),
            CIContext(),
            JobStatus(False, "ORANGE", "TESTS FAILED"),
            extracted_log=log,
        )
        # humaneval should be in evals section, not tests
        tests_section = md.split("Failed Tests")[1].split("</details>")[0]
        assert "humaneval" not in tests_section
        assert "`test_a`" in tests_section

    def test_unknown_category_shows_new_pattern(self):
        md = format_summary_markdown(
            FailureSummary(category="UNKNOWN", unknown_pattern="WEIRD_ERROR_123"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
        )
        assert "New Pattern Detected" in md
        assert "WEIRD_ERROR_123" in md

    def test_problematic_layer_shown_when_different(self):
        md = format_summary_markdown(
            FailureSummary(
                category="vllm:config",
                layer="framework",
                problematic_layer="serving",
            ),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
        )
        assert "Error Layer" in md
        assert "Problematic Layer" in md
        assert "`serving`" in md

    def test_problematic_layer_hidden_when_same(self):
        md = format_summary_markdown(
            FailureSummary(
                category="tt-metal:fabric",
                layer="framework",
                problematic_layer="framework",
            ),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
        )
        assert "Error Layer" in md
        assert "Problematic Layer" not in md

    def test_llm_stats_shown(self):
        from common.llm_client import LLMResponse

        resp = LLMResponse(
            content="{}",
            model="claude-sonnet",
            prompt_tokens=1000,
            completion_tokens=200,
            response_time_ms=1500.0,
        )
        md = format_summary_markdown(
            FailureSummary(category="app:cli"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
            llm_response=resp,
        )
        assert "AI Summary Stats" in md
        assert "claude-sonnet" in md
        assert "1,000" in md  # formatted tokens

    def test_time_after_error_shown_for_failures(self):
        log = _make_extracted(time_after_crash_seconds=300)
        md = format_summary_markdown(
            FailureSummary(category="app:cli"),
            CIContext(),
            JobStatus(False, "RED", "CRASHED"),
            extracted_log=log,
        )
        assert "Time after error" in md
        assert "5m" in md


# ── build_prompt ──────────────────────────────────────────────────────────────


class TestBuildPrompt:
    def _extracted(self) -> ExtractedLog:
        log = ExtractedLog()
        log.error_sections = ["RuntimeError: boom"]
        log.total_lines = 100
        log.extracted_lines = 10
        log.raw_lines = ["line\n"]
        return log

    def test_contains_key_sections(self):
        prompt = build_prompt(
            self._extracted(),
            CIContext(),
            categories={"categories": {"app:cli": {"description": "CLI errors", "patterns": ["argparse"]}}},
            layers={"layers": [{"name": "application", "description": "App layer", "path_patterns": ["app/"]}]},
        )
        assert "KNOWN FAILURE CATEGORIES" in prompt
        assert "STACK LAYERS" in prompt
        assert "EXTRACTED LOG" in prompt
        assert "YOUR TASK" in prompt
        assert "Return ONLY the JSON object" in prompt

    def test_categories_listed(self):
        prompt = build_prompt(
            self._extracted(),
            CIContext(),
            categories={
                "categories": {"infra:network": {"description": "Network issues", "patterns": ["DNS", "timeout"]}}
            },
            layers={"layers": []},
        )
        assert "infra:network" in prompt
        assert "Network issues" in prompt

    def test_prompt_truncated_when_too_large(self):
        log = self._extracted()
        log.error_sections = ["x" * 100_000]
        truncated = build_prompt(
            log,
            CIContext(),
            categories={"categories": {}},
            layers={"layers": []},
            max_prompt_chars=50_000,
        )
        # The error section is 100k; with a 50k limit the prompt must be cut
        assert len(truncated) <= 55_000  # some overhead
        assert "YOUR TASK" in truncated  # task section preserved
        assert "truncated" in truncated


# ── format_infra_failure_markdown ─────────────────────────────────────────────


class TestFormatInfraFailureMarkdown:
    """Test the INFRA_FAILURE markdown — must match action.yml's grep pattern."""

    def test_header_has_purple_emoji(self):
        md = format_infra_failure_markdown()
        assert "🟣" in md

    def test_header_contains_infra_failure(self):
        md = format_infra_failure_markdown()
        assert "INFRA FAILURE" in md

    def test_header_matches_action_grep_pattern(self):
        """action.yml greps for ^### .* INFRA FAILURE — this must match."""
        import re

        md = format_infra_failure_markdown()
        assert re.search(r"^### .* INFRA FAILURE", md, re.MULTILINE)

    def test_job_name_and_url_creates_link(self):
        md = format_infra_failure_markdown(
            job_name="run-release-Llama-3.1-8B-n150",
            job_url="https://github.com/org/repo/actions/runs/1/job/2",
        )
        assert "[run-release-Llama-3.1-8B-n150]" in md
        assert "(https://github.com/org/repo/actions/runs/1/job/2)" in md

    def test_job_name_without_url(self):
        md = format_infra_failure_markdown(job_name="my-job")
        assert "my-job" in md

    def test_no_metadata(self):
        md = format_infra_failure_markdown()
        assert "INFRA FAILURE" in md
        assert "Possible Causes" in md
