# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
LLM-based summarization of CI logs.

Uses the extracted log and context to generate a structured summary.
"""

import json
import sys
from dataclasses import dataclass, field

from .config import is_default_branch
from .config_context import ConfigContext, format_config_context_for_prompt
from .context import CIContext, format_context_for_prompt
from .extract import ExtractedLog, JobStatus, format_extracted_log
from common.llm_client import LLMClient, LLMResponse, get_llm_client

# Prompt size limit: ~4 chars per token, 128k context window, leave room for response
MAX_PROMPT_CHARS = 400_000

__all__ = [
    "FailureSummary",
    "SummaryResult",
    "build_prompt",
    "format_infra_failure_markdown",
    "format_summary_markdown",
    "summarize_log",
]


@dataclass
class FailureSummary:
    """Structured failure summary."""

    # Classification
    category: str = ""  # e.g., "infra:fabric", "code:assertion", "UNKNOWN"
    subcategory: str = ""  # e.g., "router_sync_timeout"
    unknown_pattern: str = ""  # If category is UNKNOWN, the pattern found

    # Location in stack
    layer: str = ""  # e.g., "driver", "framework", "model" - where error manifested
    file: str = ""  # Specific file if identified

    # Layer-aware attribution
    problematic_layer: str = ""  # Layer that CAUSED the error (may differ from layer)
    config_attribution: dict = field(default_factory=dict)  # Config that caused the error

    # Ownership
    owner_from_test_yaml: str = ""
    owner_from_codeowners: list[str] = field(default_factory=list)
    team: str = ""

    # Analysis
    is_your_code: bool | None = None  # True/False/None if uncertain
    pr_files_in_stack: list[str] = field(default_factory=list)
    root_cause: str = ""
    suggested_action: str = ""
    confidence: str = "medium"  # high/medium/low

    # LLM-determined outcome (CRASH | TESTS_FAILED | EVALS_BELOW_TARGET | SUCCESS | "")
    # Empty means the LLM didn't return one — fall back to get_job_status()
    status: str = ""

    # Raw
    error_message: str = ""
    failed_tests: list[str] = field(default_factory=list)


@dataclass
class SummaryResult:
    """Result of summarization including the summary and LLM response."""

    summary: FailureSummary
    llm_response: LLMResponse


def _truncate_prompt_if_needed(prompt: str, max_chars: int) -> str:
    """Truncate prompt if it exceeds max_chars, preserving structure."""
    if len(prompt) <= max_chars:
        return prompt

    print(f"Warning: Prompt too large ({len(prompt):,} chars), truncating to {max_chars:,}", file=sys.stderr)

    # Try to truncate the EXTRACTED LOG section while preserving YOUR TASK
    log_marker = "## EXTRACTED LOG"
    task_marker = "## YOUR TASK"

    if log_marker not in prompt:
        # Fallback: simple truncation
        return prompt[:max_chars] + "\n\n... (truncated) ...\n\nReturn ONLY the JSON object."

    before_log = prompt[: prompt.index(log_marker)]
    after_marker = prompt[prompt.index(log_marker) :]
    remaining = max_chars - len(before_log) - 500  # Leave buffer for task section

    if remaining <= 0 or task_marker not in after_marker:
        return prompt[:max_chars] + "\n\n... (truncated) ...\n\nReturn ONLY the JSON object."

    task_section = after_marker[after_marker.index(task_marker) :]
    log_section = after_marker[: after_marker.index(task_marker)]
    max_log = remaining - len(task_section)

    # Minimum log size to be useful
    min_log_size = 1000
    if max_log > min_log_size:
        log_section = log_section[:max_log] + "\n\n... (truncated due to size) ...\n\n"

    return before_log + log_section + task_section


def build_prompt(
    extracted_log: ExtractedLog,
    context: CIContext,
    categories: dict,
    layers: dict,
    config_context: ConfigContext | None = None,
    max_prompt_chars: int = MAX_PROMPT_CHARS,
) -> str:
    """Build the prompt for the LLM."""
    # Format category list for prompt
    category_list = []
    for cat_id, cat_info in categories.get("categories", {}).items():
        patterns = ", ".join(cat_info.get("patterns", [])[:5])
        category_list.append(f"  - {cat_id}: {cat_info.get('description', '')} (patterns: {patterns})")

    # Format layer list
    layer_list = []
    for layer in layers.get("layers", []):
        patterns = ", ".join(layer.get("path_patterns", [])[:3])
        layer_list.append(f"  - {layer['name']}: {layer.get('description', '')} (paths: {patterns})")

    # Format extracted configurations by layer
    config_section = ""
    if extracted_log.layer_configs:
        config_lines = ["## EXTRACTED CONFIGURATIONS"]
        lc = extracted_log.layer_configs

        if lc.application:
            config_lines.append("\n### Application Layer")
            for name, cfg in lc.application.items():
                config_lines.append(f"  - {name}: {cfg.value}")

        if lc.serving:
            config_lines.append("\n### Serving Layer (vLLM)")
            for name, cfg in lc.serving.items():
                config_lines.append(f"  - {name}: {cfg.value}")

        if lc.model:
            config_lines.append("\n### Model Layer")
            for name, cfg in lc.model.items():
                config_lines.append(f"  - {name}: {cfg.value}")

        if lc.framework:
            config_lines.append("\n### Framework Layer (tt-metal)")
            for name, cfg in lc.framework.items():
                config_lines.append(f"  - {name}: {cfg.value}")

        config_section = "\n".join(config_lines) + "\n\n"

    # Format config attribution hints if available
    attribution_hint = ""
    if extracted_log.config_attributions:
        hint_lines = ["## CONFIG ATTRIBUTION HINT"]
        hint_lines.append("The following configurations may be related to the error:")
        for attr in extracted_log.config_attributions[:3]:  # Limit to top 3
            if attr.source_config:
                layer_name = attr.source_layer.name.lower() if attr.source_layer else "unknown"
                hint_lines.append(
                    f"  - `{attr.error_param_name}` set by {layer_name} layer " f"(value: {attr.source_config.value})"
                )
                if attr.explanation:
                    hint_lines.append(f"    Explanation: {attr.explanation}")
                if attr.suggested_fix:
                    hint_lines.append(f"    Suggested fix: {attr.suggested_fix}")
        attribution_hint = "\n".join(hint_lines) + "\n\n"

    # Format config context if provided
    config_context_section = ""
    if config_context:
        config_context_section = format_config_context_for_prompt(config_context) + "\n\n"

    prompt = f"""You are a CI failure analyst. Analyze the following CI log and provide a structured summary.

## KNOWN FAILURE CATEGORIES
{chr(10).join(category_list)}

If the failure doesn't match any known category, output category as "UNKNOWN" and provide the unknown_pattern field with the error pattern you found.

## STACK LAYERS (from low to high)
{chr(10).join(layer_list)}

{config_section}## CONTEXT
{format_context_for_prompt(context)}

{config_context_section}{attribution_hint}## EXTRACTED LOG
{format_extracted_log(extracted_log)}

## YOUR TASK
Analyze the log and return a JSON object with these fields:

```json
{{
  "status": "CRASH",  // must be exactly one of: CRASH, TESTS_FAILED, EVALS_BELOW_TARGET, SUCCESS

  "category": "<category_id or UNKNOWN>",
  "subcategory": "<specific sub-type if applicable>",
  "unknown_pattern": "<if UNKNOWN, the error pattern you found>",

  "layer": "<driver|framework|operations|model|serving|application>",
  "problematic_layer": "<layer that CAUSED the error, may differ from layer where it manifested>",
  "file": "<specific file if identifiable>",

  "is_your_code": <true|false|null>,
  "pr_files_in_stack": ["<list of PR files that appear in stack trace>"],

  "root_cause": "<1-2 sentence description of what went wrong>",
  "error_message": "<single line - the exact error message to search for in logs>",
  "failed_tests": ["<list of failed test names>"],

  "suggested_action": "<SHORT action - what to do>",
  "confidence": "<high|medium|low>"
}}
```

IMPORTANT:
- Set `status` to one of exactly these values:
  * CRASH: The process itself failed — server startup error, TT_FATAL, exception, import error, argparse error, OOM kill. No tests ran.
  * TESTS_FAILED: The server started and ran, but one or more tests failed with actual test failures.
  * EVALS_BELOW_TARGET: The server ran and tests passed, but eval accuracy/benchmark scores are below target thresholds.
  * SUCCESS: Everything passed.
  NOTE: if the server failed to start (argparse error, import error, health check never passed), use CRASH — not TESTS_FAILED — even if "workflow:benchmarks" or "workflow:evals" appear as failed in the log. Those are downstream timeouts, not real test failures.
- ERROR PRECEDENCE - Root causes vs symptoms:
  * Server startup failures (argparse errors like "unrecognized arguments", import errors, config parsing errors) are ROOT CAUSES
  * Health check timeouts ("did not become healthy within Xs") are SYMPTOMS of the server not starting
  * Connection refused errors are SYMPTOMS of the server not running
  * Always identify the EARLIEST error that caused the cascade - if server failed to start, that's the root cause, not the timeout
  * Error Section 1 in the log is typically the earliest/primary error
- If the job completed and only evals failed, do NOT report warnings or info messages as the error - report the actual eval failures
- Set is_your_code to true ONLY if files from the PR/branch appear in the error stack trace
- Set is_your_code to false if the error is in infrastructure/framework code
- Set is_your_code to null if you can't determine
- If the category is UNKNOWN, make sure to fill unknown_pattern so we can add it later
- Be specific in root_cause - include device IDs, timeouts, specific error codes when relevant
- Keep suggested_action SHORT but SPECIFIC - include actual parameter names and example values when applicable
- error_message must be a SINGLE LINE that can be searched in the log
- Set problematic_layer to the layer that SET the config causing the error (e.g., if max_model_len from serving layer caused a framework crash, problematic_layer="serving")
- When suggesting fixes for config parameters, USE THE CONFIGURATION CONTEXT above to determine the correct way to set parameters (e.g., use override_tt_config instead of environment variables if that's how other runs configure it)

REQUIRED: `status` must be exactly one of: "CRASH", "TESTS_FAILED", "EVALS_BELOW_TARGET", "SUCCESS". No other values are valid.

Return ONLY the JSON object, no other text.
"""

    return _truncate_prompt_if_needed(prompt, max_prompt_chars)


def _parse_llm_response(response_text: str, extracted_log: ExtractedLog | None = None) -> FailureSummary:
    """Parse the LLM response into a FailureSummary."""
    summary = FailureSummary()

    # Extract JSON from response (handle markdown code blocks)
    json_str = response_text
    if "```json" in response_text:
        json_str = response_text.split("```json")[1].split("```")[0]
    elif "```" in response_text:
        json_str = response_text.split("```")[1].split("```")[0]

    try:
        data = json.loads(json_str.strip())

        _valid_statuses = {"CRASH", "TESTS_FAILED", "EVALS_BELOW_TARGET", "SUCCESS", ""}
        raw_status = data.get("status", "")
        if raw_status not in _valid_statuses:
            print(f"Warning: LLM returned unexpected status {raw_status!r}, ignoring", file=sys.stderr)
            raw_status = ""
        summary.status = raw_status
        summary.category = data.get("category", "")
        summary.subcategory = data.get("subcategory", "")
        summary.unknown_pattern = data.get("unknown_pattern", "")
        summary.layer = data.get("layer", "")
        summary.problematic_layer = data.get("problematic_layer", "")
        summary.file = data.get("file", "")
        summary.is_your_code = data.get("is_your_code")
        summary.pr_files_in_stack = data.get("pr_files_in_stack", [])
        summary.root_cause = data.get("root_cause", "")
        summary.error_message = data.get("error_message", "")
        summary.failed_tests = data.get("failed_tests", [])
        summary.suggested_action = data.get("suggested_action", "")
        summary.confidence = data.get("confidence", "medium")

        # If problematic_layer not set by LLM, infer from config attributions
        if not summary.problematic_layer and extracted_log and extracted_log.config_attributions:
            for attr in extracted_log.config_attributions:
                if attr.is_higher_layer_cause and attr.source_layer:
                    summary.problematic_layer = attr.source_layer.name.lower()
                    summary.config_attribution = {
                        "config_name": attr.error_param_name,
                        "config_value": attr.source_config.value if attr.source_config else "",
                        "source_layer": attr.source_layer.name.lower() if attr.source_layer else "",
                        "error_layer": attr.error_layer.name.lower() if attr.error_layer else "",
                        "explanation": attr.explanation,
                    }
                    break

    except json.JSONDecodeError as e:
        summary.root_cause = f"Failed to parse LLM response: {e}"
        summary.error_message = response_text[:500]

    return summary


def _emoji(code: str) -> str:
    """Convert text status codes to emoji circles."""
    emoji_map = {
        "RED": "🔴",
        "ORANGE": "🟠",
        "YELLOW": "🟡",
        "GREEN": "🟢",
        "PURPLE": "🟣",
        "WHITE": "⚪",
        "UNKNOWN": "❓",
    }
    return emoji_map.get(code, code)


def format_infra_failure_markdown(job_name: str = "", job_url: str = "") -> str:
    """Generate a summary for jobs that produced no workflow logs (infra failure).

    Args:
        job_name: Display name for the job. When both job_name and job_url are
            provided, the name becomes a clickable hyperlink in the header.
        job_url: URL to the GitHub Actions job page.
    """
    # Sanitize to prevent header-breaking characters (e.g. newlines from CLI input)
    job_name = job_name.strip().replace("\n", " ")
    job_url = job_url.strip().replace("\n", "")
    if job_url and job_name:
        label = f"[{job_name}]({job_url})"
    elif job_name:
        label = job_name
    elif job_url:
        label = f"<{job_url}>"
    else:
        label = ""
    header = f"### 🟣 INFRA FAILURE{' (' + label + ')' if label else ''}\n"

    return (
        header
        + """
No workflow logs were produced. The runner or inference server likely failed before generating any output.

#### Possible Causes
- Runner machine was unavailable or failed to initialize
- Docker daemon was unreachable or failed to start the container
- Network failure before any work could begin
- Job was cancelled before producing any logs

#### Suggested Action
Check the job's setup steps directly in the GitHub Actions UI for error messages from Docker pull, environment setup, or runner initialization steps.
"""
    )


def format_summary_markdown(
    summary: FailureSummary,
    context: CIContext,
    job_status: JobStatus,
    llm_response: LLMResponse | None = None,
    extracted_log: ExtractedLog | None = None,
    job_name: str = "",
    job_url: str = "",
) -> str:
    """Format the summary as markdown for display.

    job_name / job_url come from the CLI args and are authoritative.
    extracted_log values are fallbacks (extracted from log content,
    typically empty in real CI runs since we don't parse them out).
    """
    # Build header with outcome and optional job link
    link_url = job_url or (extracted_log.job_url if extracted_log else "")
    link_label = job_name or (extracted_log.job_name if extracted_log else "") or "View Job"
    if link_url:
        md = f"### {_emoji(job_status.status_code)} {job_status.status_text} ([{link_label}]({link_url}))\n"
    else:
        md = f"### {_emoji(job_status.status_code)} {job_status.status_text}\n"

    # Show problematic layer if different from error layer
    layer_info = f"| **Error Layer** | `{summary.layer}` |"
    if summary.problematic_layer and summary.problematic_layer != summary.layer:
        layer_info += f"\n| **Problematic Layer** | `{summary.problematic_layer}` |"

    # Determine "Is This Your Code?" value - only on non-default branches
    branch = context.pr.branch if context else ""
    your_code_row = ""
    if branch and not is_default_branch(branch):
        if summary.is_your_code is True:
            your_code_row = "| **Your Code?** | Yes |"
        elif summary.is_your_code is False:
            your_code_row = "| **Your Code?** | No |"
        else:
            your_code_row = "| **Your Code?** | Uncertain |"

    # Only show error details for non-successful jobs
    if not job_status.is_success:
        if summary.category:
            md += f"""
#### Classification
| Field | Value |
|-------|-------|
| **Category** | `{summary.category}` |
| **Subcategory** | `{summary.subcategory}` |
{layer_info}
| **Confidence** | {summary.confidence} |
{your_code_row}
"""

            # Show PR files in stack trace if any (still useful detail, but more compact)
            if branch and not is_default_branch(branch) and summary.pr_files_in_stack:
                md += "**Your files in stack trace:**\n"
                for f in summary.pr_files_in_stack:
                    md += f"- `{f}`\n"

            # Configuration Attribution section (only if LLM identified a specific higher-layer cause)
            if summary.config_attribution:
                attr = summary.config_attribution
                md += f"""
#### Configuration Attribution
The error manifested in **{summary.layer}** layer but was caused by configuration in **{attr.get('source_layer', 'unknown')}** layer.
| Field | Value |
|-------|-------|
| **Config Parameter** | `{attr.get('config_name', '')}` |
| **Config Value** | `{attr.get('config_value', '')}` |
| **Set By Layer** | `{attr.get('source_layer', '')}` |
| **Error In Layer** | `{attr.get('error_layer', '')}` |
"""
                if attr.get("explanation"):
                    md += f"**Why:** {attr.get('explanation')}\n"

        if summary.error_message:
            md += f"""
#### Error Message
```
{summary.error_message}
```
"""

        if summary.root_cause:
            md += f"""
#### Root Cause
{summary.root_cause}
"""

        if summary.suggested_action:
            md += f"""
#### Suggested Action
{summary.suggested_action}
"""

    # Get failed tests and evals (mutually exclusive - evals are more specific)
    failed_evals = extracted_log.failed_evals if extracted_log else []
    failed_evals_set = set(failed_evals)

    # Failed tests, excluding any that are actually eval failures
    all_failed_tests = (extracted_log.failed_tests if extracted_log else []) or summary.failed_tests
    failed_tests = [t for t in all_failed_tests if t not in failed_evals_set]

    # Show failed tests (feature/functionality failures)
    if failed_tests:
        md += "<details>\n<summary>Failed Tests</summary>\n\n"
        for test in failed_tests[:10]:
            md += f"- `{test}`\n"
        if len(failed_tests) > 10:
            md += f"- ... and {len(failed_tests) - 10} more\n"
        md += "</details>\n"

    # Show failed evals (accuracy below target)
    if failed_evals:
        md += "<details>\n<summary>Failed Evals (accuracy below target)</summary>\n\n"
        for eval_name in failed_evals[:10]:
            md += f"- `{eval_name}`\n"
        if len(failed_evals) > 10:
            md += f"- ... and {len(failed_evals) - 10} more\n"
        md += "</details>\n"

    if context.job.job_name:
        md += f"""
#### Job Info
| Field | Value |
|-------|-------|
| **Job** | `{context.job.job_name}` |
| **Team** | `{context.job.team}` |
| **Owner** | `{context.job.owner_id}` |
| **Timeout** | {context.job.timeout_minutes} min |
"""

    if summary.category == "UNKNOWN" and summary.unknown_pattern:
        md += f"""
#### New Pattern Detected
This failure pattern is not in our known categories:
```
{summary.unknown_pattern}
```
Consider adding this to `config/categories.yaml` if it recurs.
"""

    # Add stats section at the end
    stats_rows = []

    # Add timing info showing how long job ran after primary error (only for failures)
    if not job_status.is_success and extracted_log and extracted_log.time_after_crash_seconds is not None:
        from .extract import format_duration

        duration = format_duration(extracted_log.time_after_crash_seconds)
        stats_rows.append(f"| Time after error | {duration} |")

    # Add LLM usage metrics if available
    if llm_response:
        stats_rows.append(f"| LLM model | `{llm_response.model}` |")
        stats_rows.append(f"| Tokens | {llm_response.prompt_tokens:,} + {llm_response.completion_tokens:,} |")
        stats_rows.append(f"| LLM response time | {llm_response.response_time_ms:.0f}ms |")

    if stats_rows:
        md += f"""---
<details>
<summary>AI Summary Stats</summary>

| Metric | Value |
|--------|-------|
{chr(10).join(stats_rows)}
</details>
"""

    return md


def summarize_log(
    extracted_log: ExtractedLog,
    context: CIContext,
    categories: dict,
    layers: dict,
    llm_client: LLMClient | None = None,
    config_context: ConfigContext | None = None,
) -> SummaryResult:
    """
    Use LLM to summarize the log.

    Args:
        extracted_log: The extracted log content
        context: CI context (PR, job info, etc.)
        categories: Category definitions
        layers: Layer definitions
        llm_client: LLM client to use (auto-detected from env if not provided)

    Returns:
        SummaryResult containing the FailureSummary and LLMResponse
    """
    if llm_client is None:
        llm_client = get_llm_client()

    prompt = build_prompt(extracted_log, context, categories, layers, config_context)
    response = llm_client.chat(prompt, max_tokens=2000)
    summary = _parse_llm_response(response.content, extracted_log)

    # Add context info
    summary.owner_from_test_yaml = context.job.owner_id
    summary.team = context.job.team

    return SummaryResult(summary=summary, llm_response=response)
