# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Data models and status constants for ai-run-summary."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

STATUS_EMOJI: dict[str, str] = {
    "SUCCESS": "🟢",
    "CRASHED": "🔴",
    "TIMEOUT": "🔴",
    "TESTS_FAILED": "🟠",
    "FAILED": "🔴",
    "EVALS_BELOW_TARGET": "🟡",
    "INFRA_FAILURE": "🟣",
}

NON_FAILURE_STATUSES: frozenset[str] = frozenset({"SUCCESS"})


def resolve_status(status_text: str) -> str:
    """Map _job.status text to internal status code.

    Handles suffixed statuses like 'TESTS FAILED (3 failed)' and
    inconsistent formatting like 'INFRA_FAILURE' (underscore).

    Unrecognized or missing statuses collapse to FAILED — we know the job
    isn't a success (otherwise it would have said so) but the tool that
    produced the artifact gave us a label we can't map. The category field
    can still carry "unknown" for LLM-classified root-cause uncertainty;
    the *status* axis is explicitly non-UNKNOWN.
    """
    if not status_text:
        return "FAILED"
    t = status_text.strip().upper()
    if t.startswith("CRASHED"):
        return "CRASHED"
    if t.startswith("TIMEOUT"):
        return "TIMEOUT"
    if t.startswith("TESTS FAILED"):
        return "TESTS_FAILED"
    if t.startswith("EVALS BELOW TARGET"):
        return "EVALS_BELOW_TARGET"
    if t.startswith("INFRA"):
        return "INFRA_FAILURE"
    if t.startswith("FAILED"):
        return "FAILED"
    if t == "SUCCESS":
        return "SUCCESS"
    return "FAILED"


@dataclass
class ParsedJobSummary:
    """A single job summary parsed from a JSON file."""

    source_file: Path
    job_id: str = ""
    job_name: str = ""
    job_url: str = ""
    status: str = ""
    category: str = ""
    subcategory: str = ""
    layer: str = ""
    is_your_code: bool | None = None
    root_cause: str = ""
    error_message: str = ""
    confidence: str = ""
    failed_tests: list[str] = field(default_factory=list)


@dataclass
class CategoryStats:
    """Aggregated stats for a single failure category."""

    category: str
    count: int
    subcategories: Counter[str] = field(default_factory=Counter)
    job_names: list[str] = field(default_factory=list)
    is_your_code_count: int = 0


@dataclass
class RunStats:
    """Aggregated statistics across all jobs in a run."""

    total_jobs: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    failed_jobs: list[ParsedJobSummary] = field(default_factory=list)
    category_counts: list[CategoryStats] = field(default_factory=list)
    is_your_code_count: int = 0
    not_your_code_count: int = 0
    unknown_attribution: int = 0


@dataclass
class RunNarrative:
    """LLM-generated narrative for a run."""

    overall_health: str = ""
    dominant_cause: str = ""
    attribution_verdict: str = ""
    raw_response: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_time_ms: float = 0.0
