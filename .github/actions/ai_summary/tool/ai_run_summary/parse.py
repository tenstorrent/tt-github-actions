# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Parse ai-job-summary JSON output files into ParsedJobSummary objects."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .models import ParsedJobSummary, resolve_status


def parse_json_summary(file_path: Path) -> Optional[ParsedJobSummary]:
    """Parse a JSON summary file produced by ai-job-summary.

    Returns None on any parse or I/O error (with a warning to stderr).
    """
    try:
        data = json.loads(file_path.read_text())
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Warning: could not parse JSON summary {file_path}: {e}", file=sys.stderr)
        return None
    except OSError as e:
        print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
        return None

    job = data.get("_job", {})
    status = resolve_status(job.get("status", ""))
    failed_tests = job.get("failed_tests") or data.get("failed_tests") or []

    # Extract job ID from the URL path (/job/12345)
    job_url = job.get("url", "")
    job_id = ""
    if "/job/" in job_url:
        job_id = job_url.rsplit("/job/", 1)[-1].split("?")[0]

    return ParsedJobSummary(
        source_file=file_path,
        job_id=job_id,
        job_name=job.get("name", ""),
        job_url=job_url,
        status=status,
        category=data.get("category", ""),
        subcategory=data.get("subcategory", ""),
        layer=data.get("layer", ""),
        is_your_code=data.get("is_your_code"),
        root_cause=data.get("root_cause", ""),
        error_message=data.get("error_message", ""),
        confidence=data.get("confidence", ""),
        failed_tests=failed_tests,
        log_complete=job.get("log_complete"),
        run_attempt=job.get("run_attempt"),
    )


def dedup_latest_attempt(summaries: list[ParsedJobSummary]) -> list[ParsedJobSummary]:
    """Keep one summary per leg, from the latest attempt.

    A partial re-run leaves every attempt's per-job artifact on the run.
    run_attempt (GITHUB_RUN_ATTEMPT) is authoritative; job_id — the numeric
    check-run id parsed from the job URL, which GitHub assigns in creation
    order — is the tiebreak and the fallback for artifacts written before
    run_attempt was stamped. Entries with no job_name (a local run without
    --job-name) can't be keyed by leg; each is kept, appended after the keyed
    legs.

    Precondition: job names are unique within an attempt (the matrix leg name).
    Two summaries sharing a name within one attempt are a duplicate-name
    misconfig, not a re-run; the newer job_id wins and a warning is emitted so
    it's visible rather than silently collapsed.
    """

    def attempt(s: ParsedJobSummary) -> tuple[int, int]:
        ra = s.run_attempt if s.run_attempt is not None else -1
        cid = int(s.job_id) if s.job_id.isdigit() else -1
        return (ra, cid)

    best: dict[str, ParsedJobSummary] = {}
    order: list[str] = []
    passthrough: list[ParsedJobSummary] = []
    for s in summaries:
        if not s.job_name:
            passthrough.append(s)
            continue
        cur = best.get(s.job_name)
        if cur is None:
            order.append(s.job_name)
            best[s.job_name] = s
            continue
        if s.run_attempt is not None and s.run_attempt == cur.run_attempt and s.job_id != cur.job_id:
            print(
                f"::warning::two summaries share job name {s.job_name!r} within attempt "
                f"{s.run_attempt}; keeping one and dropping the other",
                file=sys.stderr,
            )
        if attempt(s) > attempt(cur):
            best[s.job_name] = s
    return [best[name] for name in order] + passthrough


def parse_summaries_dir(directory: Path) -> list[ParsedJobSummary]:
    """Scan directory for *.json summary files.

    Non-JSON files are skipped. Files that fail to parse emit a warning
    to stderr and are excluded from the returned list.
    """
    if not directory.is_dir():
        return []

    summaries: list[ParsedJobSummary] = []
    total_attempted = 0
    total_skipped = 0

    for f in sorted(directory.iterdir()):
        if not f.is_file() or f.suffix != ".json":
            continue
        total_attempted += 1
        result = parse_json_summary(f)
        if result is not None:
            summaries.append(result)
        else:
            total_skipped += 1

    if total_skipped > 0:
        print(
            f"Warning: {total_skipped} of {total_attempted} summary files could not be parsed",
            file=sys.stderr,
        )

    return summaries
