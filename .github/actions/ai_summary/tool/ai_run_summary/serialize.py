# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Machine-readable JSON serializer for ai-run-summary.

Emits the factual per-job data the report is built from, for downstream
consumers (e.g. the tt-metal digest). Deliberately omits the LLM narrative
(RunNarrative) — only objective parsed-job facts go in.
"""

from __future__ import annotations

from .models import ParsedJobSummary


def _failed_row(s: ParsedJobSummary) -> dict:
    return {
        "job_name": s.job_name,
        "job_url": s.job_url,
        "status": s.status,
        "category": s.category,
        "subcategory": s.subcategory,
        "error_message": s.error_message,
        "root_cause": s.root_cause,
        "log_complete": s.log_complete,
    }


def build_run_json(summaries: list[ParsedJobSummary], meta: dict) -> dict:
    """Build the machine-readable run JSON from parsed job summaries.

    Groups each job by its already-resolved ``status``: SUCCESS -> succeeded
    (name+url only), INFRA_FAILURE -> infra_failure, everything else -> failed.
    failed and infra_failure share a shape and keep the precise status.

    Lists are sorted by job_name to keep the artifact diff-stable across runs.
    """
    succeeded: list[dict] = []
    failed: list[ParsedJobSummary] = []
    infra: list[ParsedJobSummary] = []

    for s in summaries:
        if s.status == "SUCCESS":
            succeeded.append({"job_name": s.job_name, "job_url": s.job_url})
        elif s.status == "INFRA_FAILURE":
            infra.append(s)
        else:
            failed.append(s)

    return {
        "run_id": meta.get("run_id", ""),
        "run_url": meta.get("run_url", ""),
        "run_date": meta.get("run_date", ""),
        "run_attempt": meta.get("run_attempt"),
        "total_jobs": len(summaries),
        "succeeded": sorted(succeeded, key=lambda r: r["job_name"]),
        "failed": sorted((_failed_row(s) for s in failed), key=lambda r: r["job_name"]),
        "infra_failure": sorted((_failed_row(s) for s in infra), key=lambda r: r["job_name"]),
    }
