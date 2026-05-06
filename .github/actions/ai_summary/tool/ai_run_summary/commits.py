# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Fetch component commit SHAs for a GitHub Actions run.

Uses the gh CLI to read the resolve-shas job logs from the run and extracts
the full SHAs for tt-metal, tt-inference-server, and vllm in the order they
are resolved (matching the order defined in workflow_resolve-shas.yml).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass
class RunCommits:
    tt_metal: str = ""
    inference_server: str = ""
    vllm: str = ""


def _run_gh(*args: str) -> str:
    """Run a gh CLI command and return stdout. Returns "" on failure."""
    try:
        result = subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"Warning: gh command failed: {exc}", file=sys.stderr)
        return ""


def _find_resolve_shas_job(repo: str, run_id: int) -> int | None:
    """Return the job ID of the resolve-shas job in the run, or None."""
    page = 1
    while True:
        output = _run_gh(
            "api",
            f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}",
            "--jq",
            '.jobs[] | select(.name | test("resolve-shas|resolve.shas"; "i")) | .id',
        )
        job_ids = [line.strip() for line in output.splitlines() if line.strip()]
        if job_ids:
            try:
                return int(job_ids[0])
            except ValueError:
                return None
        # If fewer than 100 results returned, no more pages
        count_output = _run_gh(
            "api",
            f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}",
            "--jq",
            ".jobs | length",
        )
        if not count_output.strip() or int(count_output.strip() or "0") < 100:
            break
        page += 1
    return None


def fetch_run_commits(run_id: int, repo: str = "tenstorrent/tt-shield") -> RunCommits:
    """Fetch tt-metal, tt-inference-server, and vllm SHAs for a run.

    Reads the resolve-shas job logs and parses "Full sha: <sha>" lines
    in the order they appear (tt-metal, inference-server, vllm).

    Returns an empty RunCommits if the job cannot be found or logs are unavailable.
    """
    job_id = _find_resolve_shas_job(repo, run_id)
    if not job_id:
        print(f"Warning: could not find resolve-shas job for run {run_id}", file=sys.stderr)
        return RunCommits()

    logs = _run_gh("api", f"repos/{repo}/actions/jobs/{job_id}/logs")
    if not logs:
        return RunCommits()

    # Extract "Full sha: <40-char-hex>" lines in order of appearance.
    # The resolve-shas workflow resolves: tt-metal, inference-server, vllm — in that order.
    shas: list[str] = []
    for line in logs.splitlines():
        if "Full sha:" in line:
            parts = line.split("Full sha:")
            if len(parts) == 2:
                sha = parts[1].strip()
                if sha and all(c in "0123456789abcdefABCDEF" for c in sha):
                    shas.append(sha)

    commits = RunCommits()
    if len(shas) > 0:
        commits.tt_metal = shas[0]
    if len(shas) > 1:
        commits.inference_server = shas[1]
    if len(shas) > 2:
        commits.vllm = shas[2]

    return commits
