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

    md_path = file_path.with_suffix(".md")
    try:
        markdown = md_path.read_text() if md_path.exists() else ""
    except OSError:
        markdown = ""

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
        markdown=markdown,
    )


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
