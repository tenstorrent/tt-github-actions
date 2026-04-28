# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from ai_run_summary.models import ParsedJobSummary
from ai_run_summary.aggregate import compute_stats


def _job(status="CRASHED", category="", subcategory="", is_your_code=None, job_name="", failed_tests=None):
    """Helper to create ParsedJobSummary with minimal boilerplate."""
    return ParsedJobSummary(
        source_file=Path("test.json"),
        status=status,
        category=category,
        subcategory=subcategory,
        is_your_code=is_your_code,
        job_name=job_name,
        failed_tests=failed_tests or [],
    )


class TestComputeStats:
    def test_empty_input(self):
        stats = compute_stats([])
        assert stats.total_jobs == 0
        assert stats.failed_jobs == []

    def test_total_jobs(self):
        jobs = [_job(status="SUCCESS"), _job(status="CRASHED"), _job(status="TESTS_FAILED")]
        stats = compute_stats(jobs)
        assert stats.total_jobs == 3

    def test_status_counts(self):
        jobs = [_job(status="SUCCESS"), _job(status="SUCCESS"), _job(status="CRASHED")]
        stats = compute_stats(jobs)
        assert stats.status_counts == {"SUCCESS": 2, "CRASHED": 1}

    def test_failed_jobs_excludes_success(self):
        jobs = [_job(status="SUCCESS"), _job(status="CRASHED"), _job(status="TESTS_FAILED")]
        stats = compute_stats(jobs)
        assert len(stats.failed_jobs) == 2
        assert all(j.status != "SUCCESS" for j in stats.failed_jobs)

    def test_category_counts_sorted_desc(self):
        jobs = [
            _job(category="a"),
            _job(category="a"),
            _job(category="a"),
            _job(category="b"),
            _job(category="b"),
            _job(category="c"),
        ]
        stats = compute_stats(jobs)
        assert stats.category_counts[0].category == "a"
        assert stats.category_counts[0].count == 3
        assert stats.category_counts[1].count == 2

    def test_subcategory_counts(self):
        jobs = [
            _job(category="a", subcategory="x"),
            _job(category="a", subcategory="x"),
            _job(category="a", subcategory="y"),
        ]
        stats = compute_stats(jobs)
        assert stats.category_counts[0].subcategories["x"] == 2
        assert stats.category_counts[0].subcategories["y"] == 1

    def test_is_your_code_attribution(self):
        jobs = [
            _job(is_your_code=True),
            _job(is_your_code=True),
            _job(is_your_code=False),
            _job(is_your_code=None),
        ]
        stats = compute_stats(jobs)
        assert stats.is_your_code_count == 2
        assert stats.not_your_code_count == 1
        assert stats.unknown_attribution == 1

    def test_unknown_category(self):
        jobs = [_job(category="")]
        stats = compute_stats(jobs)
        assert stats.category_counts[0].category == "UNKNOWN"

    def test_job_names_collected(self):
        jobs = [_job(category="a", job_name="j1"), _job(category="a", job_name="j2")]
        stats = compute_stats(jobs)
        assert "j1" in stats.category_counts[0].job_names
        assert "j2" in stats.category_counts[0].job_names

    def test_is_your_code_per_category(self):
        jobs = [
            _job(category="a", is_your_code=True),
            _job(category="a", is_your_code=False),
            _job(category="b", is_your_code=True),
        ]
        stats = compute_stats(jobs)
        cat_a = next(c for c in stats.category_counts if c.category == "a")
        assert cat_a.is_your_code_count == 1
