# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for ai_run_summary/narrative.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_run_summary.narrative import build_run_prompt, generate_narrative, parse_narrative_response
from ai_run_summary.models import CategoryStats, ParsedJobSummary, RunNarrative, RunStats
from common.llm_client import LLMResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job(status="CRASHED", category="infra:ci", is_your_code=None, root_cause="OOM error"):
    return ParsedJobSummary(
        source_file=Path("dummy.json"),
        status=status,
        category=category,
        is_your_code=is_your_code,
        root_cause=root_cause,
        job_name="build-job",
    )


def _cat(category, count, is_your_code=0):
    return CategoryStats(
        category=category,
        count=count,
        is_your_code_count=is_your_code,
    )


def _stats(jobs=None, categories=None, is_your_code=0, not_your_code=0, unknown=0):
    jobs = jobs or []
    failures = [j for j in jobs if j.status != "SUCCESS"]
    return RunStats(
        total_jobs=len(jobs),
        status_counts={},
        failed_jobs=failures,
        category_counts=categories or [],
        is_your_code_count=is_your_code,
        not_your_code_count=not_your_code,
        unknown_attribution=unknown,
    )


def _mock_llm(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.return_value = LLMResponse(
        content=content,
        model="claude-3",
        prompt_tokens=100,
        completion_tokens=50,
        response_time_ms=1200.0,
    )
    return client


VALID_JSON_RESPONSE = """{
  "overall_health": "2 of 3 jobs failed.",
  "dominant_cause": "All failures are OOM errors in the driver layer.",
  "attribution_verdict": "Both failures were caused by PR changes."
}"""

JSON_IN_CODE_BLOCK = """```json
{
  "overall_health": "Run is mostly healthy.",
  "dominant_cause": "No dominant pattern",
  "attribution_verdict": "No PR changes caused failures."
}
```"""

INVALID_JSON_RESPONSE = "Sorry, I cannot analyze this run."

PARTIAL_JSON_RESPONSE = '{"overall_health": "Partial response"}'


# ---------------------------------------------------------------------------
# Tests: build_run_prompt
# ---------------------------------------------------------------------------


class TestBuildRunPrompt:
    def test_prompt_contains_job_count(self):
        jobs = [_job(), _job(), _job("SUCCESS")]
        stats = _stats(jobs=jobs)
        prompt = build_run_prompt(stats)
        assert "3" in prompt  # total jobs

    def test_prompt_contains_categories(self):
        categories = [_cat("infra:ci", 2), _cat("code:test", 1)]
        jobs = [_job()] * 3
        stats = _stats(jobs=jobs, categories=categories)
        prompt = build_run_prompt(stats)
        assert "infra:ci" in prompt
        assert "code:test" in prompt

    def test_prompt_contains_is_your_code_counts(self):
        jobs = [_job(is_your_code=True), _job(is_your_code=False)]
        stats = _stats(jobs=jobs, is_your_code=1, not_your_code=1, unknown=0)
        prompt = build_run_prompt(stats)
        assert "PR-caused: 1" in prompt

    def test_prompt_contains_individual_failures(self):
        jobs = [_job(root_cause="Driver OOM error")]
        stats = _stats(jobs=jobs)
        prompt = build_run_prompt(stats)
        assert "Driver OOM error" in prompt

    def test_prompt_truncates_at_20_failures(self):
        jobs = [_job(root_cause=f"Error {i}") for i in range(25)]
        stats = _stats(jobs=jobs)
        prompt = build_run_prompt(stats)
        assert "Error 0" in prompt
        assert "Error 19" in prompt
        assert "Error 24" not in prompt

    def test_prompt_truncation_note_included(self):
        jobs = [_job(root_cause=f"Error {i}") for i in range(25)]
        stats = _stats(jobs=jobs)
        prompt = build_run_prompt(stats)
        assert "Showing 20 of 25 failures" in prompt

    def test_prompt_includes_failure_count(self):
        jobs = [_job(), _job()]
        stats = _stats(jobs=jobs)
        prompt = build_run_prompt(stats)
        assert "2" in prompt

    def test_prompt_all_success_run(self):
        jobs = [_job("SUCCESS"), _job("SUCCESS")]
        stats = _stats(jobs=jobs)
        prompt = build_run_prompt(stats)
        assert "(no failures)" in prompt


# ---------------------------------------------------------------------------
# Tests: parse_narrative_response
# ---------------------------------------------------------------------------


class TestParseNarrativeResponse:
    def test_parse_valid_json(self):
        narrative = parse_narrative_response(
            VALID_JSON_RESPONSE,
            model="claude-3",
            prompt_tokens=100,
            completion_tokens=50,
            response_time_ms=1200.0,
        )
        assert narrative.overall_health == "2 of 3 jobs failed."
        assert "OOM" in narrative.dominant_cause
        assert "PR changes" in narrative.attribution_verdict

    def test_parse_json_in_code_block(self):
        narrative = parse_narrative_response(
            JSON_IN_CODE_BLOCK,
            model="gpt-4",
            prompt_tokens=200,
            completion_tokens=60,
            response_time_ms=900.0,
        )
        assert narrative.overall_health == "Run is mostly healthy."
        assert narrative.dominant_cause == "No dominant pattern"
        assert narrative.attribution_verdict == "No PR changes caused failures."

    def test_parse_invalid_json_returns_fallback(self):
        narrative = parse_narrative_response(
            INVALID_JSON_RESPONSE,
            model="gpt-4",
            prompt_tokens=50,
            completion_tokens=10,
            response_time_ms=500.0,
        )
        assert isinstance(narrative, RunNarrative)
        assert narrative.overall_health != ""
        assert INVALID_JSON_RESPONSE in narrative.raw_response
        # Fallback leaves other fields empty
        assert narrative.dominant_cause == ""
        assert narrative.attribution_verdict == ""

    def test_parse_partial_json_missing_keys(self):
        # JSON with only one of the three expected keys
        narrative = parse_narrative_response(
            PARTIAL_JSON_RESPONSE,
            model="x",
            prompt_tokens=0,
            completion_tokens=0,
            response_time_ms=0.0,
        )
        assert narrative.overall_health == "Partial response"
        assert narrative.dominant_cause == ""
        assert narrative.attribution_verdict == ""

    def test_metadata_populated(self):
        narrative = parse_narrative_response(
            VALID_JSON_RESPONSE,
            model="claude-3",
            prompt_tokens=100,
            completion_tokens=50,
            response_time_ms=1200.0,
        )
        assert narrative.model == "claude-3"
        assert narrative.prompt_tokens == 100
        assert narrative.completion_tokens == 50
        assert narrative.response_time_ms == 1200.0

    def test_raw_response_preserved(self):
        narrative = parse_narrative_response(
            VALID_JSON_RESPONSE,
            model="x",
            prompt_tokens=0,
            completion_tokens=0,
            response_time_ms=0.0,
        )
        assert narrative.raw_response == VALID_JSON_RESPONSE


# ---------------------------------------------------------------------------
# Tests: generate_narrative
# ---------------------------------------------------------------------------


class TestGenerateNarrative:
    def test_returns_run_narrative(self):
        jobs = [_job()]
        stats = _stats(jobs=jobs)
        client = _mock_llm(VALID_JSON_RESPONSE)
        result = generate_narrative(stats, llm_client=client)
        assert isinstance(result, RunNarrative)

    def test_llm_called_exactly_once(self):
        jobs = [_job()]
        stats = _stats(jobs=jobs)
        client = _mock_llm(VALID_JSON_RESPONSE)
        generate_narrative(stats, llm_client=client)
        assert client.chat.call_count == 1

    def test_llm_error_raises(self):
        jobs = [_job()]
        stats = _stats(jobs=jobs)
        client = MagicMock()
        client.chat.side_effect = RuntimeError("API error")
        with pytest.raises(RuntimeError, match="API error"):
            generate_narrative(stats, llm_client=client)

    def test_narrative_fields_from_response(self):
        jobs = [_job()]
        stats = _stats(jobs=jobs)
        client = _mock_llm(VALID_JSON_RESPONSE)
        result = generate_narrative(stats, llm_client=client)
        assert result.overall_health == "2 of 3 jobs failed."
        assert result.model == "claude-3"

    def test_prompt_passed_to_llm(self):
        jobs = [_job(root_cause="Specific OOM error")]
        stats = _stats(jobs=jobs)
        client = _mock_llm(VALID_JSON_RESPONSE)
        generate_narrative(stats, llm_client=client)
        prompt_arg = client.chat.call_args.args[0]
        assert "Specific OOM error" in prompt_arg

    def test_auto_detect_llm_client_when_none(self):
        jobs = [_job()]
        stats = _stats(jobs=jobs)
        mock_client = _mock_llm(VALID_JSON_RESPONSE)
        with patch("ai_run_summary.narrative.get_llm_client", return_value=mock_client) as mock_get:
            generate_narrative(stats, llm_client=None)
            mock_get.assert_called_once()
