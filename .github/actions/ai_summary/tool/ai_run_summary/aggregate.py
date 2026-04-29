# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Aggregate per-job summaries into run-level statistics."""

from __future__ import annotations

from collections import Counter

from .models import CategoryStats, NON_FAILURE_STATUSES, ParsedJobSummary, RunStats


def compute_stats(summaries: list[ParsedJobSummary]) -> RunStats:
    """Compute aggregated statistics from a list of parsed job summaries."""
    if not summaries:
        return RunStats()

    stats = RunStats(total_jobs=len(summaries))

    for job in summaries:
        stats.status_counts[job.status] = stats.status_counts.get(job.status, 0) + 1

    stats.failed_jobs = [j for j in summaries if j.status not in NON_FAILURE_STATUSES]

    category_map: dict[str, list[ParsedJobSummary]] = {}
    for job in stats.failed_jobs:
        cat = job.category or "UNKNOWN"
        category_map.setdefault(cat, []).append(job)

    for cat, jobs in category_map.items():
        subcategories: Counter[str] = Counter()
        job_names: list[str] = []
        is_your_code_count = 0

        for job in jobs:
            if job.subcategory:
                subcategories[job.subcategory] += 1
            job_names.append(job.job_name)
            if job.is_your_code is True:
                is_your_code_count += 1

        stats.category_counts.append(
            CategoryStats(
                category=cat,
                count=len(jobs),
                subcategories=subcategories,
                job_names=job_names,
                is_your_code_count=is_your_code_count,
            )
        )

    stats.category_counts.sort(key=lambda c: c.count, reverse=True)

    for job in stats.failed_jobs:
        if job.is_your_code is True:
            stats.is_your_code_count += 1
        elif job.is_your_code is False:
            stats.not_your_code_count += 1
        else:
            stats.unknown_attribution += 1

    return stats
