# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""LLM-generated narrative for a CI run."""

from __future__ import annotations

import json
import re
import sys

from common.llm_client import LLMClient, get_llm_client
from .models import RunNarrative, RunStats

# Maximum number of individual failures included in the prompt to avoid token bloat.
_MAX_FAILURES_IN_PROMPT = 20

# Maximum tokens to request for the three-field JSON response.
# 800 gives enough room for three moderately detailed sentences without over-spending
# tokens; the original 500 was sometimes too tight for verbose model responses.
_NARRATIVE_MAX_TOKENS = 800

# Maximum characters of a raw LLM response used as the fallback overall_health value.
_FALLBACK_HEALTH_MAX_LEN = 200


def build_run_prompt(stats: RunStats) -> str:
    """Build the LLM prompt for run-level analysis.

    The prompt instructs the LLM to return a JSON object with exactly three keys:
        overall_health, dominant_cause, attribution_verdict

    Individual failures are capped at _MAX_FAILURES_IN_PROMPT entries to limit
    prompt size.

    Args:
        stats: Aggregated run statistics.

    Returns:
        Prompt string ready to send to the LLM.
    """
    failure_count = len(stats.failed_jobs)
    total = stats.total_jobs

    # Status overview
    status_lines = [f"  - {status}: {count}" for status, count in sorted(stats.status_counts.items())]
    status_section = "\n".join(status_lines) or "  (no jobs)"

    # Category distribution
    cat_lines = []
    for cat in stats.category_counts:
        cat_lines.append(
            f"  - {cat.category}: {cat.count} failures " f"(PR-caused: {cat.is_your_code_count}/{cat.count})"
        )
    cat_section = "\n".join(cat_lines) or "  (no failures)"

    # Attribution summary
    attribution = (
        f"PR-caused: {stats.is_your_code_count}, "
        f"pre-existing: {stats.not_your_code_count}, "
        f"unknown: {stats.unknown_attribution}"
    )

    # Per-failure details (capped at _MAX_FAILURES_IN_PROMPT)
    failures_to_include = stats.failed_jobs[:_MAX_FAILURES_IN_PROMPT]
    failure_lines = []
    for i, job in enumerate(failures_to_include, 1):
        failure_lines.append(
            f"  {i}. [{job.status}] {job.job_name or 'unknown'} "
            f"| category: {job.category or 'UNKNOWN'} "
            f"| is_your_code: {job.is_your_code} "
            f"| root_cause: {job.root_cause or '(none)'}"
        )
    failures_section = "\n".join(failure_lines) or "  (no failures)"

    truncation_note = ""
    if len(stats.failed_jobs) > _MAX_FAILURES_IN_PROMPT:
        truncation_note = f"\n(Showing {_MAX_FAILURES_IN_PROMPT} of {len(stats.failed_jobs)} failures)"

    prompt = f"""You are a CI analysis expert. Analyze the following CI run statistics and provide a concise run-level summary.

## RUN STATISTICS
Total jobs: {total}
Failed jobs: {failure_count}

### Job Status Counts
{status_section}

### Failure Category Distribution
{cat_section}

### Attribution Summary
{attribution}

### Individual Failures
{failures_section}{truncation_note}

## YOUR TASK
Analyze the run and return a JSON object with exactly these fields:

```json
{{
  "overall_health": "<1-sentence description of overall run health>",
  "dominant_cause": "<shared root cause across failures, or 'No dominant pattern'>",
  "attribution_verdict": "<1-sentence PR vs pre-existing failure assessment>"
}}
```

Be concise and specific. Return ONLY the JSON object, no other text.
"""
    return prompt


def parse_narrative_response(
    response_text: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    response_time_ms: float,
) -> RunNarrative:
    """Parse LLM response into a RunNarrative.

    Handles:
    - Direct JSON response
    - JSON wrapped in ```json...``` code block
    - Invalid JSON -> fallback narrative with overall_health set to a truncated
      version of the raw response and dominant_cause/attribution_verdict empty.

    Args:
        response_text: Raw LLM response string.
        model: Model name from the LLM response.
        prompt_tokens: Prompt token count.
        completion_tokens: Completion token count.
        response_time_ms: Wall-clock time for the LLM call.

    Returns:
        RunNarrative populated from the response (or a fallback on parse failure).
    """
    # Strip code block markers if present, using a greedy inner match to handle
    # nested braces in JSON values (e.g. "dominant_cause": "timeout in {device}").
    text = response_text.strip()
    code_block_match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    if code_block_match:
        text = code_block_match.group(1)

    try:
        data = json.loads(text)
        return RunNarrative(
            overall_health=data.get("overall_health", ""),
            dominant_cause=data.get("dominant_cause", ""),
            attribution_verdict=data.get("attribution_verdict", ""),
            raw_response=response_text,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            response_time_ms=response_time_ms,
        )
    except (json.JSONDecodeError, ValueError):
        # Fallback: surface raw response as overall_health so the report still
        # contains something useful, and leave other narrative fields empty.
        print(f"Warning: LLM response was not valid JSON, using raw response as fallback", file=sys.stderr)
        return RunNarrative(
            overall_health=response_text[:_FALLBACK_HEALTH_MAX_LEN]
            if response_text
            else "Unable to generate narrative.",
            dominant_cause="",
            attribution_verdict="",
            raw_response=response_text,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            response_time_ms=response_time_ms,
        )


def generate_narrative(stats: RunStats, llm_client: LLMClient | None = None) -> RunNarrative:
    """Generate an LLM narrative for a run.

    Args:
        stats: Aggregated run statistics.
        llm_client: LLM client. Auto-detected from environment variables if not provided.

    Returns:
        RunNarrative with LLM-generated analysis.

    Raises:
        RuntimeError: If the LLM call fails.
        ValueError: If no LLM credentials are configured and llm_client is None.
    """
    if llm_client is None:
        llm_client = get_llm_client()

    prompt = build_run_prompt(stats)
    response = llm_client.chat(prompt, max_tokens=_NARRATIVE_MAX_TOKENS)

    return parse_narrative_response(
        response_text=response.content,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        response_time_ms=response.response_time_ms,
    )
