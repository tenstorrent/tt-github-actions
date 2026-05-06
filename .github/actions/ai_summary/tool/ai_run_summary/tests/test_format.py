# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for ai_run_summary/format.py."""

from collections import Counter
from pathlib import Path

from ai_run_summary.format import (
    _extract_run_label,
    _group_by_main_category,
    _job_url,
    _progress_bar,
    format_run_report,
)
from ai_run_summary.models import CategoryStats, ParsedJobSummary, RunNarrative, RunStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job(
    status="SUCCESS",
    category="",
    job_name="job",
    job_url="",
    root_cause="",
    source_stem="65000000001",
    job_id="",
    is_your_code=None,
):
    return ParsedJobSummary(
        source_file=Path(f"/staging/{source_stem}.md"),
        job_id=job_id,
        status=status,
        category=category,
        job_name=job_name,
        job_url=job_url,
        root_cause=root_cause,
        is_your_code=is_your_code,
    )


def _stats(jobs=None, categories=None, is_your_code=0, not_your_code=0, unknown=0):
    jobs = jobs or []
    failures = [j for j in jobs if j.status != "SUCCESS"]
    status_counts: dict[str, int] = {}
    for j in jobs:
        status_counts[j.status] = status_counts.get(j.status, 0) + 1
    return RunStats(
        total_jobs=len(jobs),
        status_counts=status_counts,
        failed_jobs=failures,
        category_counts=categories or [],
        is_your_code_count=is_your_code,
        not_your_code_count=not_your_code,
        unknown_attribution=unknown,
    )


def _narrative(**kwargs):
    defaults = dict(
        overall_health="Run looks healthy.",
        dominant_cause="No dominant pattern",
        attribution_verdict="No PR changes caused failures.",
        raw_response="{}",
        model="claude-3",
        prompt_tokens=100,
        completion_tokens=50,
        response_time_ms=1200.0,
    )
    defaults.update(kwargs)
    return RunNarrative(**defaults)


def _cat(category, count, is_your_code=0, subs=None):
    return CategoryStats(
        category=category,
        count=count,
        subcategories=Counter(subs or {}),
        is_your_code_count=is_your_code,
    )


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestProgressBar:
    def test_full_bar(self):
        assert _progress_bar(100, 20) == "\u2588" * 20

    def test_empty_bar(self):
        assert _progress_bar(0, 20) == "\u2591" * 20

    def test_half_bar(self):
        bar = _progress_bar(50, 20)
        assert bar.count("\u2588") == 10
        assert bar.count("\u2591") == 10

    def test_width_respected(self):
        assert len(_progress_bar(30, 10)) == 10


class TestExtractModel:
    def test_strips_run_release_prefix_keeps_device(self):
        # Device suffix is kept -- it shows which platform the job ran on
        label = _extract_run_label(_job(job_name="run-release-Llama-3.1-8B-Instruct-n150-n150"))
        assert label == "Llama-3.1-8B-Instruct-n150-n150"
        assert "Llama-3.1-8B-Instruct" in label

    def test_keeps_device_suffix_intact(self):
        label = _extract_run_label(_job(job_name="run-release-Qwen2.5-VL-7B-Instruct-t3k"))
        assert "t3k" in label
        assert "Qwen2.5-VL-7B-Instruct" in label

    def test_extracts_from_root_cause_when_job_name_generic(self):
        job = _job(
            job_name="run-tests-with-inference-server", root_cause="Tokenizer failed for Llama-3.3-70B-Instruct model"
        )
        assert _extract_run_label(job) == "Llama-3.3-70B-Instruct"

    def test_returns_empty_when_not_found(self):
        job = _job(job_name="run-tests-with-inference-server", root_cause="HTTP 500 on /tt-liveness endpoint")
        assert _extract_run_label(job) == ""


class TestJobUrl:
    def test_uses_existing_job_url_when_has_job_path(self):
        job = _job(job_url="https://github.com/example/runs/1/job/42")
        assert _job_url(job) == "https://github.com/example/runs/1/job/42"

    def test_reconstructs_url_from_run_url_and_job_id(self):
        job = _job(job_id="65000042", job_url="")
        url = _job_url(job, run_url="https://github.com/example/runs/1")
        assert url == "https://github.com/example/runs/1/job/65000042"

    def test_empty_job_id_does_not_reconstruct(self):
        job = _job(job_url="", job_id="")
        assert _job_url(job, run_url="https://github.com/example/runs/1") == ""


class TestGroupByMainCategory:
    def test_groups_by_prefix(self):
        cats = [
            _cat("tt-metal:memory", 5),
            _cat("tt-metal:trace", 4),
            _cat("vllm:config", 3),
        ]
        groups = _group_by_main_category(cats)
        mains = [g[0] for g in groups]
        assert mains[0] == "tt-metal"
        assert mains[1] == "vllm"

    def test_subcategories_accumulated(self):
        cats = [_cat("tt-metal:memory", 5), _cat("tt-metal:trace", 4)]
        groups = _group_by_main_category(cats)
        _, count, subs, _ = groups[0]
        assert count == 9
        assert subs["memory"] == 5
        assert subs["trace"] == 4

    def test_no_subcategory_category(self):
        cats = [_cat("UNKNOWN", 3)]
        groups = _group_by_main_category(cats)
        assert groups[0][0] == "UNKNOWN"
        assert groups[0][2] == {}


# ---------------------------------------------------------------------------
# Integration tests: format_run_report
# ---------------------------------------------------------------------------


class TestFormatRunReport:
    def test_title_is_always_ai_run_summary(self):
        report = format_run_report(RunStats())
        assert "## AI Run Summary" in report.md

    def test_run_details_line_with_url_and_id(self):
        report = format_run_report(
            RunStats(),
            run_url="https://github.com/example/runs/99",
            run_id="99",
        )
        assert "[99](https://github.com/example/runs/99)" in report.md

    def test_run_details_with_date(self):
        report = format_run_report(RunStats(), run_id="99", run_date="2026-03-13")
        assert "2026-03-13" in report.md

    def test_status_table_has_progress_bar(self):
        stats = _stats(jobs=[_job("SUCCESS"), _job("CRASHED")])
        report = format_run_report(stats)
        assert "\u2588" in report.md
        assert "\u2591" in report.md

    def test_status_sorted_by_severity(self):
        jobs = [_job("SUCCESS"), _job("CRASHED"), _job("TESTS_FAILED"), _job("INFRA_FAILURE")]
        stats = _stats(jobs=jobs)
        report = format_run_report(stats)
        infra_pos = report.md.index("INFRA_FAILURE")
        crashed_pos = report.md.index("CRASHED")
        tests_pos = report.md.index("TESTS_FAILED")
        success_pos = report.md.index("SUCCESS")
        assert infra_pos < crashed_pos < tests_pos < success_pos

    def test_category_grouped_by_prefix(self):
        cats = [_cat("tt-metal:memory", 5), _cat("tt-metal:trace", 3), _cat("vllm:config", 2)]
        stats = _stats(jobs=[_job("CRASHED")] * 10, categories=cats)
        report = format_run_report(stats)
        assert "| **`tt-metal`** |" in report.md
        assert "| **`vllm`** |" in report.md
        # Subcategories inline, not as separate rows
        assert "memory 5" in report.md
        assert "trace 3" in report.md
        assert "| `tt-metal:memory` |" not in report.md
        assert "\u21b3" not in report.md

    def test_pr_impact_absent_for_scheduled_run(self):
        stats = _stats(jobs=[_job("CRASHED")], is_your_code=1)
        report = format_run_report(stats, pr="")
        assert "PR Impact" not in report.md

    def test_pr_impact_present_when_pr_set(self):
        stats = _stats(jobs=[_job("CRASHED")], is_your_code=1)
        report = format_run_report(stats, pr="42")
        assert "### PR Impact" in report.md
        assert "#42" in report.md

    def test_category_has_bars(self):
        cats = [_cat("tt-metal:memory", 5)]
        stats = _stats(jobs=[_job("CRASHED")] * 5, categories=cats)
        report = format_run_report(stats)
        assert "\u2588" in report.md

    def test_stats_footer_present(self):
        report = format_run_report(RunStats())
        assert "<summary>Run Summary Stats</summary>" in report.md

    def test_narrative_present(self):
        stats = _stats(jobs=[_job("SUCCESS")])
        report = format_run_report(stats, narrative=_narrative(overall_health="All good."))
        assert "All good." in report.md

    def test_narrative_absent_without_narrative(self):
        stats = _stats(jobs=[_job("SUCCESS")])
        report = format_run_report(stats, narrative=None)
        assert "> " not in report.md

    def test_no_failed_job_details_section(self):
        stats = _stats(jobs=[_job("CRASHED"), _job("SUCCESS")])
        report = format_run_report(stats)
        assert "Failed Job Details" not in report.md

    def test_commit_sha_section_absent_when_not_provided(self):
        report = format_run_report(RunStats())
        assert "TT-Metal" not in report.md
        assert "tt-inference-server" not in report.md
        assert "vLLM" not in report.md

    def test_tt_metal_commit_renders_short_sha_with_link(self):
        sha = "abcdef1234567890abcdef1234567890abcdef12"
        report = format_run_report(RunStats(), tt_metal_commit=sha)
        assert "**TT-Metal**" in report.md
        assert f"[`abcdef1`](https://github.com/tenstorrent/tt-metal/commit/{sha})" in report.md

    def test_vllm_commit_renders(self):
        sha = "1234567890abcdef1234567890abcdef12345678"
        report = format_run_report(RunStats(), vllm_commit=sha)
        assert "**vLLM**" in report.md
        assert f"[`1234567`]" in report.md

    def test_inference_server_commit_renders(self):
        sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        report = format_run_report(RunStats(), inference_server_commit=sha)
        assert "**tt-inference-server**" in report.md
        assert "[`deadbee`]" in report.md

    def test_model_details_absent_when_not_provided(self):
        stats = _stats(jobs=[_job("SUCCESS")])
        report = format_run_report(stats)
        assert "Model Details" not in report.md

    def test_model_details_present_when_all_summaries_given(self):
        jobs = [_job("SUCCESS", job_name="run-release-Llama-3.1-8B-Instruct-n150"), _job("CRASHED")]
        stats = _stats(jobs=jobs)
        report = format_run_report(stats, all_summaries=jobs)
        assert "Model Details (2)" in report.md

    def test_model_details_sorted_alphabetically(self):
        jobs = [
            _job("SUCCESS", job_name="run-release-Zephyr-7B-n150", source_stem="z"),
            _job("CRASHED", job_name="run-release-Llama-3.1-8B-n150", source_stem="l"),
        ]
        stats = _stats(jobs=jobs)
        report = format_run_report(stats, all_summaries=jobs)
        llama_pos = report.md.index("Llama-3.1-8B-n150")
        zephyr_pos = report.md.index("Zephyr-7B-n150")
        assert llama_pos < zephyr_pos
