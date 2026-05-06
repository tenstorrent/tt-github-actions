# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Report renderer for ai-run-summary."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import markdown

from .models import NON_FAILURE_STATUSES, ParsedJobSummary, RunNarrative, RunStats, STATUS_EMOJI


@dataclass
class RunReport:
    """Run report in both markdown and HTML formats."""

    md: str
    html: str


# Maximum characters of root_cause shown in the Failed Job Details table.
_ROOT_CAUSE_COL_MAX = 90

# Width (block chars) for the status overview bar.
_STATUS_BAR_WIDTH = 20

# Width (block chars) for the category breakdown bars.
_CAT_BAR_WIDTH = 12

# Status sort order: most severe first (purple -> red -> orange -> yellow -> green).
_STATUS_PRIORITY: dict[str, int] = {
    "INFRA_FAILURE": 0,
    "CRASHED": 1,
    "TIMEOUT": 1,
    "FAILED": 1,
    "TESTS_FAILED": 2,
    "EVALS_BELOW_TARGET": 3,
    "SUCCESS": 5,
}

# Job names that are generic CI step names, not meaningful model identifiers.
_GENERIC_JOB_NAMES = {
    "run-tests-with-inference-server",
    "run-evals",
    "run-benchmarks",
    "run-model",
}

# Prefixes stripped when extracting a human-readable model label from job_name.
_JOB_NAME_PREFIXES = ("run-release-", "run-evals-", "run-sdxl-", "run-")

# Regex to extract a model name from free-text fields (root_cause, error_message).
_MODEL_IN_TEXT_RE = re.compile(
    r"\b("
    r"Llama-[\d][\w.-]*"
    r"|Qwen[\d]?[-\.][\w.-]+"
    r"|FLUX\.[\d][\w.-]*"
    r"|Mistral-[\w.-]+"
    r"|Mixtral-[\w.-]+"
    r"|Gemma-[\w.-]+"
    r"|Phi-[\w.-]+"
    r"|DeepSeek-[\w.-]+"
    r"|Whisper-[\w.-]+"
    r"|stable-diffusion-[\w.-]+"
    r"|Mochi-[\w.-]+"
    r"|Mochi(?=[\s,)])"
    r"|Motif(?=[\s,)])"
    r"|unet(?=[\s,)])"
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _progress_bar(pct: float, width: int) -> str:
    """Return a Unicode block-char progress bar for the given percentage (0-100)."""
    filled = round(pct / 100 * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _extract_run_label(job: ParsedJobSummary) -> str:
    """Return the most informative run label for a job, or "" if unknown.

    When job_name is the full matrix job name (set via job-name: ${{ github.job }}
    in the calling workflow), strips only the CI prefix (e.g. "run-release-") and
    keeps the model + device portion intact so the platform is visible.
    e.g. "run-release-Llama-3.1-8B-Instruct-llmbox-t3k" -> "Llama-3.1-8B-Instruct-llmbox-t3k"

    Falls back to extracting just the model name from root_cause / error_message
    when the job_name is not available or is a generic step name.
    """
    name = (job.job_name or "").strip()
    # Skip purely numeric names (job IDs used as fallback) and known generic step names
    if name and not name.isdigit() and name not in _GENERIC_JOB_NAMES:
        for prefix in _JOB_NAME_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        # Keep the device/platform suffix -- it's the information the user wants
        if name:
            return name

    # Fallback: extract model name from free-text fields
    for field in (job.root_cause, job.error_message):
        if field:
            m = _MODEL_IN_TEXT_RE.search(field)
            if m:
                return m.group(1)

    return ""


def _job_url(job: ParsedJobSummary, run_url: str = "") -> str:
    """Return the best available direct URL for the job."""
    if job.job_url and "/job/" in job.job_url:
        return job.job_url
    if run_url and job.job_id:
        return f"{run_url}/job/{job.job_id}"
    return job.job_url or ""


def _job_id_cell(job: ParsedJobSummary, run_url: str = "") -> str:
    """Render the Job column as a linked job ID."""
    label = job.job_id or job.source_file.stem.removeprefix("ai_job_summary_")
    url = _job_url(job, run_url)
    return f"[{label}]({url})" if url else label


def _group_by_main_category(
    category_counts: list,
) -> list[tuple[str, int, dict[str, int], int]]:
    """Collapse category:subcategory pairs into top-level groups.

    Returns [(main, total_count, {sub: count}, is_your_code_count)] sorted desc.
    """
    groups: dict[str, dict] = {}
    for cat in category_counts:
        parts = cat.category.split(":", 1)
        main = parts[0]
        sub = parts[1] if len(parts) > 1 else ""
        if main not in groups:
            groups[main] = {"count": 0, "subs": defaultdict(int), "your_code": 0}
        groups[main]["count"] += cat.count
        groups[main]["your_code"] += cat.is_your_code_count
        if sub:
            groups[main]["subs"][sub] += cat.count

    return sorted(
        [(k, v["count"], dict(v["subs"]), v["your_code"]) for k, v in groups.items()],
        key=lambda x: x[1],
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _job_expandable_block(
    job: ParsedJobSummary,
    run_url: str,
    show_root_cause: bool = True,
    compact: bool = False,
) -> str:
    """Render a single job as a <details> block containing the full .md summary.

    compact=True shows only model, status and job link (no category / root cause).
    """
    emoji = STATUS_EMOJI.get(job.status, "")
    model = _extract_run_label(job) or "\u2014"
    url = _job_url(job, run_url)
    label = job.job_id or job.source_file.stem.removeprefix("ai_job_summary_")
    job_link = f'<a href="{url}">{label}</a>' if url else label

    parts = [f"<strong>{model}</strong>", f"{emoji} {job.status}", job_link]

    if not compact:
        category = job.category or "UNKNOWN"
        parts.append(f"<code>{category}</code>")

        if show_root_cause:
            root_cause = (job.root_cause or "")
            if len(root_cause) > _ROOT_CAUSE_COL_MAX:
                root_cause = root_cause[:_ROOT_CAUSE_COL_MAX] + "\u2026"
            parts.append(root_cause)

    summary_line = " &nbsp;|&nbsp; ".join(parts)

    body = job.markdown.strip() if job.markdown else ""
    if not body:
        body = f"**Root Cause**: {job.root_cause}" if job.root_cause else f"Status: {job.status}"
    elif compact:
        # Strip the trailing "AI Summary Stats" <details> block (and its --- separator)
        body = re.sub(
            r"\s*-{3,}\s*<details>\s*<summary>AI Summary Stats</summary>.*$",
            "",
            body,
            flags=re.DOTALL,
        ).strip()

    return f"<details>\n<summary>{summary_line}</summary>\n\n{body}\n\n</details>\n\n"


def format_run_report(
    stats: RunStats,
    narrative: RunNarrative | None = None,
    run_url: str = "",
    run_id: str = "",
    run_date: str = "",
    pr: str = "",
    tt_metal_commit: str = "",
    vllm_commit: str = "",
    inference_server_commit: str = "",
    all_summaries: list | None = None,
) -> "RunReport":
    """Render a complete run-level report (markdown + HTML).

    Args:
        stats: Aggregated run statistics.
        narrative: Optional LLM-generated narrative.
        run_url: GitHub Actions run URL.
        run_id: Run identifier string.
        run_date: Human-readable run date (e.g. "2026-03-13"). Omitted if empty.
        pr: PR number or branch name. When set, shows PR Impact section and
            "Your Code" column in job details.
        tt_metal_commit: Optional TT-Metal commit SHA.
        vllm_commit: Optional vLLM commit SHA.
        inference_server_commit: Optional tt-inference-server commit SHA.
        all_summaries: All parsed job summaries (success + failure). When provided,
            a Model Details section is appended after the failed job details.

    Returns:
        RunReport with .md and .html attributes.
    """
    md = ""

    # -----------------------------------------------------------------------
    # 1. Title
    # -----------------------------------------------------------------------
    md += "## AI Run Summary\n\n"

    # -----------------------------------------------------------------------
    # 2. Run details line
    # -----------------------------------------------------------------------
    details_parts = []
    if run_url and run_id:
        details_parts.append(f"**Run**: [{run_id}]({run_url})")
    elif run_id:
        details_parts.append(f"**Run**: {run_id}")
    elif run_url:
        details_parts.append(f"**Run**: {run_url}")
    if run_date:
        details_parts.append(f"**Date**: {run_date}")
    if pr:
        details_parts.append(f"**PR**: #{pr}")
    if details_parts:
        md += " \u00b7 ".join(details_parts) + "\n\n"

    # -----------------------------------------------------------------------
    # 3. Component versions (commit SHAs)
    # -----------------------------------------------------------------------
    versions = []
    if tt_metal_commit:
        short = tt_metal_commit[:7]
        url = f"https://github.com/tenstorrent/tt-metal/commit/{tt_metal_commit}"
        versions.append(f"**TT-Metal**: [`{short}`]({url})")
    if inference_server_commit:
        short = inference_server_commit[:7]
        url = f"https://github.com/tenstorrent/tt-inference-server/commit/{inference_server_commit}"
        versions.append(f"**tt-inference-server**: [`{short}`]({url})")
    if vllm_commit:
        short = vllm_commit[:7]
        url = f"https://github.com/tenstorrent/vllm/commit/{vllm_commit}"
        versions.append(f"**vLLM**: [`{short}`]({url})")
    if versions:
        md += " \u00b7 ".join(versions) + "\n\n"

    # -----------------------------------------------------------------------
    # 4. Overall Health (LLM sentence)
    # -----------------------------------------------------------------------
    if narrative and narrative.overall_health:
        md += f"> {narrative.overall_health}\n\n"

    # -----------------------------------------------------------------------
    # 4. Job Status Overview -- sorted by severity, with visual bar
    # -----------------------------------------------------------------------
    md += "### Job Status Overview\n\n"
    md += "| Status | Count | Distribution |\n"
    md += "|--------|-------|--------------|\n"

    total = stats.total_jobs
    if stats.status_counts:
        for status, count in sorted(
            stats.status_counts.items(),
            key=lambda kv: (_STATUS_PRIORITY.get(kv[0], 99), kv[0]),
        ):
            emoji = STATUS_EMOJI.get(status, "")
            pct = count / total * 100 if total > 0 else 0
            bar = _progress_bar(pct, _STATUS_BAR_WIDTH)
            md += f"| {emoji} {status} | {count} | `{bar}` {pct:.0f}% |\n"
    else:
        md += "| \u2014 | 0 | \u2014 |\n"
    md += "\n"

    # -----------------------------------------------------------------------
    # 5. Failure Category Distribution
    #    Main categories: bar showing % of total failures
    #    Subcategories (indented): bar showing % of their parent
    # -----------------------------------------------------------------------
    if stats.category_counts:
        total_failures = len(stats.failed_jobs)
        md += "### Failure Category Distribution\n\n"
        md += "| Category | Jobs | Distribution | Subcategories |\n"
        md += "|----------|------|--------------|---------------|\n"

        for main, count, subs, _ in _group_by_main_category(stats.category_counts):
            pct = count / total_failures * 100 if total_failures > 0 else 0
            bar = _progress_bar(pct, _CAT_BAR_WIDTH)
            breakdown = (
                " \u00b7 ".join(f"{sub} {n}" for sub, n in sorted(subs.items(), key=lambda x: -x[1]))
                if subs
                else "\u2014"
            )
            md += f"| **`{main}`** | **{count}** | `{bar}` {pct:.0f}% | {breakdown} |\n"

        md += "\n"

    # -----------------------------------------------------------------------
    # 6. PR Impact -- only for PR / branch runs
    # -----------------------------------------------------------------------
    if pr and stats.failed_jobs:
        md += "### PR Impact\n\n"
        md += f"- **PR-caused failures**: {stats.is_your_code_count}\n"
        md += f"- **Pre-existing / infrastructure failures**: {stats.not_your_code_count}\n"
        md += f"- **Attribution unknown**: {stats.unknown_attribution}\n"
        if narrative and narrative.attribution_verdict:
            md += f"\n{narrative.attribution_verdict}\n"
        md += "\n"

    # -----------------------------------------------------------------------
    # 7. Dominant Failure Pattern (LLM)
    # -----------------------------------------------------------------------
    if narrative and narrative.dominant_cause:
        md += "### Dominant Failure Pattern\n\n"
        md += f"{narrative.dominant_cause}\n\n"

    # -----------------------------------------------------------------------
    # 8. Failed Job Details -- sorted by status severity then category
    #    Job column: always the numeric job ID linked to the job URL
    #    Model column: extracted model name, or "--" when not identifiable
    # -----------------------------------------------------------------------
    if stats.failed_jobs:
        show_your_code = bool(pr)
        sorted_failures = sorted(
            stats.failed_jobs,
            key=lambda j: (
                _STATUS_PRIORITY.get(j.status, 99),
                j.category or "UNKNOWN",
            ),
        )

        if show_your_code:
            header = "| Job | Run | Status | Category | Your Code | Root Cause |"
            sep = "|-----|-----|--------|----------|-----------|------------|"
        else:
            header = "| Job | Run | Status | Category | Root Cause |"
            sep = "|-----|-----|--------|----------|------------|"

        md += f"<details>\n<summary>Failed Job Details ({len(sorted_failures)})</summary>\n\n"
        md += header + "\n"
        md += sep + "\n"

        for job in sorted_failures:
            job_cell = _job_id_cell(job, run_url)
            model = _extract_run_label(job) or "\u2014"
            emoji = STATUS_EMOJI.get(job.status, "")
            category = job.category or "UNKNOWN"
            root_cause = (job.root_cause or "").replace("|", "\\|")
            if len(root_cause) > _ROOT_CAUSE_COL_MAX:
                root_cause = root_cause[:_ROOT_CAUSE_COL_MAX] + "\u2026"

            if show_your_code:
                yc = "Yes" if job.is_your_code is True else ("No" if job.is_your_code is False else "?")
                md += f"| {job_cell} | {model} | {emoji} {job.status} | `{category}` | {yc} | {root_cause} |\n"
            else:
                md += f"| {job_cell} | {model} | {emoji} {job.status} | `{category}` | {root_cause} |\n"

        md += "\n</details>\n\n"

    # -----------------------------------------------------------------------
    # 10. Model Details -- all jobs alphabetically, each expandable
    # -----------------------------------------------------------------------
    if all_summaries is not None:
        sorted_by_model = sorted(
            all_summaries,
            key=lambda j: (_extract_run_label(j) or "").lower(),
        )
        md += f"<details>\n<summary>Model Details ({len(sorted_by_model)} jobs)</summary>\n\n"
        for job in sorted_by_model:
            md += _job_expandable_block(job, run_url, compact=True)
        md += "</details>\n\n"

    # -----------------------------------------------------------------------
    # 11. Stats footer
    # -----------------------------------------------------------------------
    footer_rows = [
        f"| Total jobs | {stats.total_jobs} |",
        f"| Failed | {len(stats.failed_jobs)} |",
        f"| Passed | {stats.status_counts.get('SUCCESS', 0)} |",
    ]
    if narrative:
        footer_rows += [
            f"| LLM model | `{narrative.model}` |",
            f"| Tokens | {narrative.prompt_tokens} + {narrative.completion_tokens} |",
            f"| LLM time | {narrative.response_time_ms:.0f}ms |",
        ]

    md += "<details>\n<summary>Run Summary Stats</summary>\n\n"
    md += "| Metric | Value |\n"
    md += "|--------|-------|\n"
    md += "\n".join(footer_rows) + "\n"
    md += "\n</details>\n"

    html = _render_html(md)
    return RunReport(md=md, html=html)


def _render_html(md_text: str) -> str:
    """Convert markdown to self-contained styled HTML."""
    body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    return (
        "<!DOCTYPE html>\n"
        "<html><head><meta charset='utf-8'><style>\n"
        "body { background: #1a1a2e; color: #e0e0e0; "
        "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; "
        "font-size: 28px; padding: 48px 64px; margin: 0; line-height: 1.6; }\n"
        "h2 { color: #fff; font-size: 1.4em; margin-bottom: 8px; }\n"
        "h3 { color: #ddd; font-size: 1.1em; margin-top: 32px; }\n"
        "table { border-collapse: collapse; margin: 16px 0 24px; }\n"
        "th { color: #999; font-weight: 600; text-align: left; padding: 12px 20px; border-bottom: 3px solid #333; }\n"
        "td { padding: 12px 20px; border-bottom: 2px solid #2a2a3e; }\n"
        "code { background: #2d2d44; padding: 4px 10px; border-radius: 6px; font-size: 0.85em; white-space: nowrap; }\n"
        "blockquote { border-left: 4px solid #555; padding-left: 20px; margin-left: 0; color: #bbb; }\n"
        "details { margin-top: 24px; }\n"
        "summary { cursor: pointer; color: #999; font-weight: 600; }\n"
        "a { color: #6ea8fe; text-decoration: none; }\n"
        f"</style></head><body>{body}</body></html>"
    )
