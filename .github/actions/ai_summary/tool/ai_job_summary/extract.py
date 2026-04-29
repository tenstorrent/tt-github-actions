# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Log extraction - smart extraction without brittle positional heuristics.

Strategy:
1. Find error/failure markers and extract with context
2. Use GitHub Actions structural markers (##[group], ##[error])
3. Deduplicate repeated warnings
4. Keep the extraction format-agnostic
5. Extract layer configurations for error attribution
"""

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .extract_configs import (
    ConfigAttribution,
    ConfigLayer,
    LayerConfigs,
    TrackedConfig,
)


# Maximum line length before truncation (very long lines are often JSON with embedded logs)
MAX_LINE_LENGTH = 1000

# Timestamp pattern for logs - supports both formats:
# - ISO format: 2026-02-10T13:51:41 (GitHub Actions)
# - Python logging: 2026-02-10 13:51:41,305
TIMESTAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})")


def parse_timestamp(line: str) -> datetime | None:
    """Extract and parse timestamp from a log line."""
    match = TIMESTAMP_PATTERN.search(line)
    if match:
        try:
            return datetime.fromisoformat(match.group(1))
        except ValueError:
            return None
    return None


def format_duration(seconds: int) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


@dataclass
class ExtractedLog:
    """Result of log extraction."""

    job_info: str = ""  # First meaningful lines (job name, branch, etc.)
    error_sections: list[str] = field(default_factory=list)
    final_status: str = ""  # Last section with results
    deduplicated_warnings: dict[str, int] = field(default_factory=dict)
    total_lines: int = 0
    extracted_lines: int = 0

    # Job metadata
    job_name: str = ""
    job_url: str = ""
    timestamp: str = ""
    exit_code: int | None = None
    has_crash: bool = False  # TT_FATAL, panic, etc.
    has_timeout: bool = False
    failed_tests: list[str] = field(default_factory=list)  # Test failures (features missing)
    failed_evals: list[str] = field(default_factory=list)  # Eval failures (accuracy below target)

    # Timing information (for TT-Metal crashes)
    crash_timestamp: str = ""  # When the TT-Metal crash occurred
    job_end_timestamp: str = ""  # When the job finished
    time_after_crash_seconds: int | None = None  # How long job ran after crash

    # Layer-aware configuration tracking
    layer_configs: LayerConfigs | None = None  # Configs extracted from each layer
    config_attributions: list[ConfigAttribution] = field(default_factory=list)  # Error-to-config correlations

    # Raw log content (stored to avoid re-reading, especially for merged directories)
    raw_lines: list[str] = field(default_factory=list)


@dataclass
class JobStatus:
    """Job outcome status - single source of truth for success/failure determination."""

    is_success: bool
    status_code: str  # RED, ORANGE, YELLOW, GREEN, PURPLE
    status_text: str  # SUCCESS, CRASHED, TIMEOUT, TESTS FAILED, etc.


# Single source of truth for the INFRA_FAILURE display status. cli.py
# constructs this in 4 different code paths; centralizing here keeps the
# spelling consistent. Display form matches the other multi-word status
# values ("TESTS FAILED", "EVALS BELOW TARGET").
INFRA_FAILURE_STATUS = JobStatus(False, "PURPLE", "INFRA FAILURE")


# Maps LLM enum values (from the prompt schema) to the display JobStatus.
# Keys are what the LLM returns; values are the corresponding JobStatus.
_LLM_STATUS_MAP: dict[str, JobStatus] = {
    "CRASH": JobStatus(False, "RED", "CRASHED"),  # LLM says "CRASH", displayed as "CRASHED"
    "TESTS_FAILED": JobStatus(False, "ORANGE", "TESTS FAILED"),
    "EVALS_BELOW_TARGET": JobStatus(False, "YELLOW", "EVALS BELOW TARGET"),
    "SUCCESS": JobStatus(True, "GREEN", "SUCCESS"),
}


def apply_llm_status(job_status: JobStatus, llm_status: str) -> JobStatus:
    """
    Apply the LLM's status determination, taking precedence over extraction.

    Extraction sees raw signals; the LLM understands causality. When they
    disagree, the LLM wins. When they agree on TESTS_FAILED or EVALS_BELOW_TARGET,
    keep the extraction version because it includes the count in the label.

    TIMEOUT from extraction is always preserved — timeouts are authoritative
    infrastructure signals the LLM may not see in truncated logs.
    """
    if not llm_status:
        return job_status

    if llm_status not in _LLM_STATUS_MAP:
        print(f"Warning: unknown LLM status {llm_status!r}, ignoring", file=sys.stderr)
        return job_status

    # Timeouts are authoritative — never let LLM SUCCESS upgrade them.
    if job_status.status_text == "TIMEOUT":
        return job_status

    new_status = _LLM_STATUS_MAP[llm_status]

    # If extraction already agrees, keep its version (it may include counts
    # in the label, e.g. "TESTS FAILED (3/10)").
    if job_status.status_text.startswith(new_status.status_text):
        return job_status

    return new_status


def get_job_status(extracted_log: ExtractedLog) -> JobStatus:
    """
    Determine initial job outcome status from extracted log metadata.

    Returns the extraction-based status. May be overridden by apply_llm_status()
    after the LLM call when the LLM identifies a different root cause
    (e.g. server never started → CRASH rather than TESTS FAILED).
    """
    if extracted_log.has_crash:
        return JobStatus(False, "RED", "CRASHED")
    if extracted_log.has_timeout:
        return JobStatus(False, "RED", "TIMEOUT")
    if extracted_log.exit_code is not None and extracted_log.exit_code != 0:
        if extracted_log.failed_tests:
            return JobStatus(False, "ORANGE", f"TESTS FAILED ({len(extracted_log.failed_tests)} failed)")
        return JobStatus(False, "RED", f"FAILED (exit code {extracted_log.exit_code})")
    if extracted_log.failed_tests:
        return JobStatus(False, "ORANGE", f"TESTS FAILED ({len(extracted_log.failed_tests)} failed)")
    if extracted_log.failed_evals:
        return JobStatus(False, "YELLOW", f"EVALS BELOW TARGET ({len(extracted_log.failed_evals)} failed)")
    return JobStatus(True, "GREEN", "SUCCESS")


# Patterns that indicate important content (case-insensitive where needed)

# TT-Metal error codes - these are ALWAYS extracted, never dropped
# These represent actual crashes/assertions in the TT-Metal stack
TT_METAL_ERROR_PATTERNS = [
    r"TT_FATAL",
    r"TT_THROW",
    r"TT_ASSERT",
    r"RuntimeError:\s*TT_",  # RuntimeError from TT-Metal
    r"tt_metal.*assert",  # Assertions in tt_metal code
    r"ttnn.*assert",  # Assertions in ttnn code
]

# High priority patterns - important but can be dropped if too many
HIGH_PRIORITY_PATTERNS = [
    *TT_METAL_ERROR_PATTERNS,
    r"\bpanic\b",
    r"##\[error\]",  # GitHub Actions error
    r"\[critical\]",
    r"FAILED\s*\]",  # gtest failure
    r"Process completed with exit code [1-9]",  # Final GHA status only
    r"Segmentation fault",
    r"SIGSEGV",
    r"SIGABRT",
]

# Error patterns - be specific to avoid false positives
# Avoid matching config values like "timeout-minutes: 360" or "exit code" in info messages
ERROR_PATTERNS = [
    r"(?:^|[:\s])error(?:[:\s]|$)",  # "error:" or " error " but not "error_handler"
    r"\bfailed\b",  # "failed" specifically
    r"\bfailure\b",  # "failure" specifically
    r"\bfatal\b",
    r"\bexception\b",
    r"\btimed\s*out\b",  # "timed out" (past tense) not "timeout" (config)
    r"\bcrash(?:ed)?\b",
    r"\bassert(?:ion)?\s*(?:failed|error)",  # "assertion failed" not just "assert"
    r"AssertionError",
    r"RuntimeError",
    r"ValueError",
    r"TypeError",
    r"KeyError",
    r"IndexError",
    r"AttributeError",
    *HIGH_PRIORITY_PATTERNS,
]

# Patterns for warnings (to deduplicate)
WARNING_PATTERNS = [
    r"\bwarning\b",
    r"\[warning\]",
]

# GitHub Actions structural markers
GHA_GROUP_START = r"##\[group\]"
GHA_GROUP_END = r"##\[endgroup\]"
GHA_ERROR = r"##\[error\]"

# Patterns to identify job context (usually at start)
JOB_CONTEXT_PATTERNS = [
    r"Runner name:",
    r"Complete job name:",
    r"Uses:",
    r"Current branch:",
    r"workflow",
    r"docker-image:",
]

# Patterns for final status (usually at end)
FINAL_STATUS_PATTERNS = [
    r"PASSED.*tests",
    r"FAILED.*tests",
    r"\[\s*PASSED\s*\]",
    r"\[\s*FAILED\s*\]",
    r"exit code",
    r"Job completed",
    r"##\[error\]",
    r"timed out",
    r"Process completed with exit code",
    r"passed,.*failed",
    r"Error:",
    r"SUCCESS",
    r"FAILURE",
]


def merge_log_files(log_dirs: Path | list[Path]) -> list[str]:
    """
    Merge all .log files from one or more directories by timestamp.

    Lines without recognized timestamps are kept with their preceding timestamped
    line from the same file (context preservation).

    Args:
        log_dirs: Single directory or list of directories containing log files

    Returns:
        List of lines merged and sorted by timestamp, with source markers
    """
    if isinstance(log_dirs, Path):
        log_dirs = [log_dirs]

    # Collect (log_file, source_label) pairs from all dirs
    file_pairs: list[tuple[Path, str]] = []
    for log_dir in log_dirs:
        for log_file in sorted(log_dir.rglob("*.log")):
            source = log_file.relative_to(log_dir).as_posix()
            # Prefix with dir name to distinguish files from different dirs
            if len(log_dirs) > 1:
                source = f"{log_dir.name}/{source}"
            file_pairs.append((log_file, source))

    if not file_pairs:
        return []

    # Read each file, tracking timestamps
    # Each entry: (sort_key, line_index, line, source)
    # sort_key is (timestamp, file_index, line_index) for stable sorting
    all_entries: list[tuple[tuple, int, str, str]] = []

    for file_idx, (log_file, source) in enumerate(file_pairs):
        last_ts: datetime | None = None

        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                for line_idx, line in enumerate(f):
                    ts = parse_timestamp(line)
                    if ts is not None:
                        last_ts = ts

                    # Use last known timestamp for sorting (or epoch if none yet)
                    sort_ts = last_ts or datetime.min

                    # Sort key: (timestamp, file_index, line_index) for stable ordering
                    sort_key = (sort_ts, file_idx, line_idx)
                    all_entries.append((sort_key, line_idx, line, source))
        except Exception:
            continue

    if not all_entries:
        return []

    # Sort by the composite key
    all_entries.sort(key=lambda x: x[0])

    # Build merged output with source markers
    merged: list[str] = []
    current_source: str | None = None

    for sort_key, line_idx, line, source in all_entries:
        if source != current_source:
            merged.append(f"[{source}]\n")
            current_source = source
        merged.append(line)

    return merged


def extract_log(
    log_source: Path | list[Path],
    context_lines: int = 5,
    max_error_sections: int = 100,  # max seed error lines; output sections may be fewer after merging
    max_chars: int = 200_000,  # ~50k tokens, well under 64k limit
    test_patterns: dict | None = None,
    config_patterns: dict | None = None,
) -> ExtractedLog:
    """
    Extract important parts from a CI log.

    Args:
        log_source: A log file, directory, or list of directories.
                    Directories have their .log files merged by timestamp.
        context_lines: Number of lines before/after errors to include
        max_error_sections: Maximum number of error sections to extract
        max_chars: Maximum total characters in extracted content
        test_patterns: Test result patterns
        config_patterns: Configuration extraction patterns for layer-aware attribution

    Returns:
        ExtractedLog with extracted sections and layer configs
    """
    if isinstance(log_source, list):
        lines = merge_log_files(log_source)
        if not lines:
            raise ValueError(f"No .log files found in {log_source}")
        first_path = log_source[0]
    elif log_source.is_dir():
        lines = merge_log_files(log_source)
        if not lines:
            raise ValueError(f"No .log files found in {log_source}")
        first_path = log_source
    else:
        with open(log_source, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        first_path = log_source

    result = ExtractedLog(total_lines=len(lines))
    result.raw_lines = lines

    # Extract timestamp from first lines
    for line in lines[:50]:
        if match := re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line):
            result.timestamp = match.group(1)
            break

    # Scan full log for metadata
    full_text = "".join(lines)

    # Detect infrastructure crashes - these are ALWAYS the root cause, not test failures.
    # TT_THROW/TT_FATAL means tt-metal crashed, which cascades to vLLM timeout and test failures.
    # Python exceptions anchored to line start are also crashes (process died on uncaught exception).
    tt_crash = re.search(
        r"TT_FATAL|TT_THROW|\bpanic\b|Segmentation fault|SIGSEGV",
        full_text,
        re.IGNORECASE,
    )
    # The (?<![\w.]) lookbehind allows real log prefixes ("(APIServer pid=N) ",
    # "[core.py:1104] ") while excluding module-qualified or concatenated
    # names (vllm.RuntimeError:, MyAttributeError:). AssertionError /
    # ValueError / TypeError / IndexError are intentionally absent — they
    # appear routinely in pytest E-lines from healthy test failures.
    py_crash = re.search(
        r"(?<![\w.])(?:AttributeError|KeyError|RuntimeError|ModuleNotFoundError|ImportError):",
        full_text,
    )
    result.has_crash = bool(tt_crash or py_crash)

    # Detect timeout - be specific to avoid false positives from config values
    # Only match actual timeout EVENTS (past tense "timed out"), not config values ("timeout: 60s")
    timeout_patterns = [
        r"##\[error\].*timed\s*out",  # GitHub Actions error with timeout
        r"(?:job|test|request|process|operation)\s+timed\s*out",  # "X timed out"
        r"timed\s*out\s+(?:after|waiting)",  # "timed out after/waiting"
        r"exceeded\s+time\s+limit",  # Time limit exceeded
        r"cancelled\s+due\s+to\s+timeout",  # Cancelled due to timeout
    ]
    result.has_timeout = bool(re.search("|".join(timeout_patterns), full_text, re.IGNORECASE))

    # Extract exit code - ONLY from the final GitHub Actions status line
    # Avoid matching random "exit code" mentions in logs (e.g., hugepages service)
    if match := re.search(r"Process completed with exit code (\d+)", full_text):
        result.exit_code = int(match.group(1))

    # Use provided test patterns or default to empty
    if test_patterns is None:
        test_patterns = {}

    # Detect if tests actually ran and completed (even with failures)
    # This helps distinguish "crash during tests" from "tests ran, some failed"
    tests_ran = False
    tests_passed = 0

    # Apply configurable test result patterns
    for pattern_config in test_patterns.get("test_result_patterns", []):
        pattern = pattern_config.get("pattern", "")
        pattern_type = pattern_config.get("type", "")

        if match := re.search(pattern, full_text, re.IGNORECASE):
            if pattern_type == "return_codes":
                # Comma-separated return codes, 0 = pass, non-zero = fail
                codes = [int(x.strip()) for x in match.group(1).split(",") if x.strip().isdigit()]
                if codes:
                    tests_ran = True
                    tests_passed = codes.count(0)
                    # If any return code is non-zero, set exit_code to indicate failure
                    non_zero = [c for c in codes if c != 0]
                    if non_zero and result.exit_code is None:
                        result.exit_code = non_zero[0]  # Use first non-zero code
            elif pattern_type == "passed_failed":
                # Group 1 = passed count, group 2 = failed count
                tests_ran = True
                tests_passed = int(match.group(1))

            if tests_ran:
                break  # Use first matching pattern

    # Determine if crash/timeout should be treated as root cause or symptom
    # Key insight: TT_THROW/TT_FATAL is ALWAYS the root cause - it crashes tt-metal,
    # which causes vLLM to timeout, which causes tests to fail. The test failures are symptoms.
    #
    # Only downgrade timeout to "cleanup issue" if:
    # 1. Tests ran and some passed, AND
    # 2. There was NO infrastructure crash (TT_THROW/TT_FATAL)
    if tests_ran and tests_passed > 0 and not result.has_crash:
        result.has_timeout = False  # Timeout was likely during cleanup, not the real failure

    # Extract failed eval names first (accuracy/performance below targets)
    # Pattern: "| eval_name | FAIL " in results tables
    failed_evals = set()
    for match in re.finditer(r"\|\s*(\S+)\s*\|\s*FAIL\s*", full_text):
        eval_name = match.group(1)
        # Skip if it looks like a header or model name
        if eval_name not in ("Model", "Device", "Eval", "Status") and not eval_name.startswith("--"):
            failed_evals.add(eval_name)
    result.failed_evals = sorted(failed_evals)[:20]  # Cap at 20

    # Extract failed test names using configurable patterns
    # Exclude any that are already classified as eval failures
    failed_tests = set()
    for pattern_config in test_patterns.get("failed_test_patterns", []):
        pattern = pattern_config.get("pattern", "")
        prefix = pattern_config.get("prefix", "")

        for match in re.finditer(pattern, full_text, re.IGNORECASE):
            test_name = match.group(1)
            if prefix:
                test_name = f"{prefix}{test_name}"
            # Only add if not already an eval failure
            if test_name not in failed_evals:
                failed_tests.add(test_name)

    result.failed_tests = sorted(failed_tests)[:20]  # Cap at 20

    # Compile patterns (case-insensitive)
    error_re = re.compile("|".join(ERROR_PATTERNS), re.IGNORECASE)
    warning_re = re.compile("|".join(WARNING_PATTERNS), re.IGNORECASE)
    job_context_re = re.compile("|".join(JOB_CONTEXT_PATTERNS))
    final_status_re = re.compile("|".join(FINAL_STATUS_PATTERNS))

    # Track which lines we've extracted
    extracted_indices = set()
    warning_counts = Counter()

    # Pass 1: Find job context (first 200 lines)
    job_context_lines = []
    for i, line in enumerate(lines[:200]):
        if job_context_re.search(line):
            line_content = _smart_truncate_long_line(line.rstrip(), max_length=MAX_LINE_LENGTH)
            job_context_lines.append(line_content)
            extracted_indices.add(i)
    result.job_info = "\n".join(job_context_lines[:20])  # Cap at 20 lines

    # Pass 2: Find errors with priority system
    # Priority 1: TT-Metal errors (ALWAYS included - these are the root cause)
    # Priority 2: Other errors, prioritized by position (earlier = more important)
    tt_metal_re = re.compile("|".join(TT_METAL_ERROR_PATTERNS), re.IGNORECASE)

    tt_metal_indices = []  # ALWAYS included
    other_error_indices = []  # Prioritized by position (earlier first)
    seen_patterns = set()

    for i, line in enumerate(lines):
        if i in extracted_indices:
            continue

        # Check for warnings (deduplicate)
        if warning_re.search(line) and not error_re.search(line):
            warning_counts[_normalize_warning(line)] += 1
            continue

        # Check for errors
        if error_re.search(line):
            # Keep first occurrence of each error pattern
            pattern_key = _normalize_error(line)
            if pattern_key in seen_patterns:
                continue
            seen_patterns.add(pattern_key)

            # TT-Metal errors are ALWAYS included (they're the root cause)
            if tt_metal_re.search(line):
                tt_metal_indices.append(i)
            else:
                other_error_indices.append(i)

    # Combine: ALL TT-Metal errors + fill remainder with other errors
    # Other errors are already in line-number order (earlier = higher priority)
    remaining_slots = max(0, max_error_sections - len(tt_metal_indices))
    selected_indices = tt_metal_indices + other_error_indices[:remaining_slots]

    # Sort by line number for coherent output (earlier errors first)
    selected_indices = sorted(selected_indices)

    # Merge overlapping ranges so consecutive error lines (e.g. a stack trace)
    # become one section instead of many
    ranges = []
    for i in selected_indices:
        start = max(0, i - context_lines)
        end = min(len(lines), i + context_lines + 1)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    # Extract merged sections
    error_sections = []
    total_chars = len(result.job_info)

    for start, end in ranges:
        section_lines = []
        for j in range(start, end):
            if j not in extracted_indices:
                line = lines[j].rstrip()
                # Smart truncation preserves error info from very long lines (e.g., JSON blobs)
                line = _smart_truncate_long_line(line, max_length=MAX_LINE_LENGTH)
                section_lines.append(f"{j+1}: {line}")
                extracted_indices.add(j)

        if section_lines:
            section = "\n".join(section_lines)
            error_sections.append(section)
            total_chars += len(section)

            if total_chars >= max_chars:
                break

    result.error_sections = _dedupe_error_sections(error_sections)

    # Note: Time-after-error calculation is done in a second pass after LLM identifies
    # the primary error. See calculate_time_after_error() function.

    # Pass 3: Find final status (last 300 lines)
    final_lines = []
    start_idx = max(0, len(lines) - 300)
    for i, line in enumerate(lines[-300:], start=start_idx):
        if final_status_re.search(line) or GHA_ERROR in line:
            # Get some context
            start = max(0, i - 2)
            end = min(len(lines), i + 3)
            for j in range(start, end):
                if j not in extracted_indices:
                    line_content = lines[j].rstrip()
                    line_content = _smart_truncate_long_line(line_content, max_length=MAX_LINE_LENGTH)
                    final_lines.append(line_content)
                    extracted_indices.add(j)

    result.final_status = "\n".join(final_lines[-50:])  # Cap at 50 lines

    # Store deduplicated warnings (only those with count > 1)
    result.deduplicated_warnings = {w: c for w, c in warning_counts.most_common(10) if c > 1}

    result.extracted_lines = len(extracted_indices)

    # Extract layer configurations for error attribution
    if config_patterns:
        result.layer_configs = extract_layer_configs(lines, config_patterns)

        # Correlate errors with configs
        # Use all error sections to find correlations
        error_text = "\n".join(error_sections) + "\n" + full_text
        if result.has_crash:
            # For crashes, default error layer is framework
            result.config_attributions = correlate_error_with_configs(
                error_text,
                result.layer_configs,
                ConfigLayer.FRAMEWORK,
                config_patterns,
            )

    return result


def _smart_truncate_long_line(line: str, max_length: int = MAX_LINE_LENGTH) -> str:
    """
    Intelligently truncate very long lines, preserving important information.

    For JSON blobs: extract error-related fields
    For other lines: keep start + end (middle is often repetitive retry logs)
    """
    if len(line) <= max_length:
        return line

    # Try to detect and handle JSON blobs
    stripped = line.strip()
    if stripped.startswith("{") or '"error"' in line or '"message"' in line:
        try:
            # Find the JSON object in the line
            start = line.find("{")
            if start >= 0:
                prefix = line[:start]
                json_str = line[start:]
                # Try to parse as JSON
                data = json.loads(json_str)
                # Extract only error-relevant fields
                extracted = {}
                for key in ["error", "message", "exception", "status", "code", "reason", "detail"]:
                    if key in data:
                        val = data[key]
                        # Truncate nested values too
                        if isinstance(val, str) and len(val) > 500:
                            val = val[:500] + "..."
                        extracted[key] = val
                if extracted:
                    return prefix + json.dumps(extracted) + " ... (JSON truncated)"
        except (json.JSONDecodeError, ValueError, TypeError):
            pass  # Not valid JSON, use fallback

    # Fallback: keep start and end, cut the middle.
    # Rationale: Error messages typically have useful context at both ends:
    # - Start: timestamp, error type, test name
    # - End: actual exception message, stack trace bottom
    # - Middle: often 100+ repetitive retry attempts
    truncation_marker = " ... [truncated] ... "
    keep_start = max_length // 2
    keep_end = max_length // 2 - len(truncation_marker)
    return line[:keep_start] + truncation_marker + line[-keep_end:]


def _normalize_line(line: str) -> str:
    """Normalize a log line by removing volatile fragments for dedup.

    Strips: timestamps (`YYYY-MM-DDTHH:MM:SS[.ms][Z]`), hex addresses (`0x...`),
    process/thread IDs (both `pid: 123` colon and `pid=123` equals forms),
    device IDs (`physical_device_id: N`), inline line numbers (`:N:`,
    `line N`), temp paths (`/tmp/...`), and retry/iteration counters. Input
    is expected to be a raw log line, not an `extract_log`-formatted section
    line — section-level prefix stripping lives in `_dedupe_error_sections`.
    """
    # Remove timestamps
    line = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*Z?", "", line)
    # Remove memory addresses
    line = re.sub(r"0x[0-9a-fA-F]+", "0x...", line)
    # Remove PIDs/TIDs (both "pid: 123" and "pid=123" formats — real vLLM /
    # tt-metal logs use the equals form: "(EngineCore_DP0 pid=197)").
    # Anchor with a non-word lookbehind so we don't match "rapid=200" etc.
    line = re.sub(r"(?<!\w)pid[:=]\s*\d+", "pid=...", line)
    line = re.sub(r"(?<!\w)tid[:=]\s*\d+", "tid=...", line)
    # Remove device IDs that vary
    line = re.sub(r"physical_device_id:\s*\d+", "physical_device_id: N", line)
    # Remove line numbers
    line = re.sub(r":\d+:", ":N:", line)
    line = re.sub(r"line \d+", "line N", line)
    # Remove file paths with varying parts
    line = re.sub(r"/tmp/[^\s]+", "/tmp/...", line)
    # Normalize retry/iteration patterns to improve deduplication.
    # When a test retries 100+ times, each attempt is logged separately but
    # represents the same failure - normalizing these helps deduplication.
    line = re.sub(r"attempt\s+\d+/\d+", "attempt N/M", line)
    line = re.sub(r"retry\s+\d+\s+of\s+\d+", "retry N of M", line, flags=re.IGNORECASE)
    line = re.sub(r"\(\d+/\d+\)", "(N/M)", line)
    line = re.sub(r"\[\d+/\d+\]", "[N/M]", line)
    line = re.sub(r"#\d+", "#N", line)
    return line.strip()


def _normalize_warning(line: str) -> str:
    """Normalize a warning line for deduplication."""
    return _normalize_line(line)[:200]


def _normalize_error(line: str) -> str:
    """Normalize an error line for deduplication."""
    return _normalize_line(line)[:150]


# Section lines produced by extract_log are prefixed with "{line_number}: "
# (see the `f"{j+1}: {line}"` assembly in extract_log). Strip that before
# normalization so identical errors at different log positions dedupe.
_SECTION_LINE_PREFIX = re.compile(r"^\d+:\s*")


def _normalize_section_line(line: str) -> str:
    """Like _normalize_line but also strips the extract_log section prefix."""
    return _normalize_line(_SECTION_LINE_PREFIX.sub("", line))


def _dedupe_error_sections(sections: list[str]) -> list[str]:
    """Collapse identical error sections (modulo timestamps/PIDs/addresses).

    Normalizes each line via _normalize_section_line (which strips the
    extract_log "{lineno}: " prefix plus the usual volatile fragments),
    hashes the joined normalized text, and keeps only the first occurrence.
    Duplicates are acknowledged inline: '... (N identical occurrences omitted)'.
    """
    if len(sections) < 2:
        return sections

    seen: dict[str, int] = {}  # normalized text -> index of first occurrence
    counts: dict[int, int] = {}  # index -> number of duplicates found after it
    kept: list[int] = []  # ordered list of indices we keep

    for i, section in enumerate(sections):
        normalized = "\n".join(_normalize_section_line(line) for line in section.splitlines())
        if normalized in seen:
            counts[seen[normalized]] = counts.get(seen[normalized], 0) + 1
        else:
            seen[normalized] = i
            kept.append(i)

    result: list[str] = []
    for i in kept:
        if i in counts:
            n = counts[i]
            noun = "occurrence" if n == 1 else "occurrences"
            result.append(f"{sections[i]}\n... ({n} identical {noun} omitted)")
        else:
            result.append(sections[i])
    return result


def extract_layer_configs(
    lines: list[str],
    config_patterns: dict | None = None,
) -> LayerConfigs:
    """
    Extract configuration values from log lines organized by layer.

    Args:
        lines: Log file lines
        config_patterns: Patterns from config/config_patterns.yaml

    Returns:
        LayerConfigs with configs organized by layer
    """
    if config_patterns is None:
        config_patterns = {}

    layer_configs = LayerConfigs()
    full_text = "".join(lines)

    # Process each layer's patterns
    # Support both old format (application_patterns) and new format (application)
    layer_mapping = {
        "application": (ConfigLayer.APPLICATION, layer_configs.application),
        "serving": (ConfigLayer.SERVING, layer_configs.serving),
        "model": (ConfigLayer.MODEL, layer_configs.model),
        "framework": (ConfigLayer.FRAMEWORK, layer_configs.framework),
    }

    for pattern_key, (layer, config_dict) in layer_mapping.items():
        # Try new format first, then fall back to old format with _patterns suffix
        patterns = config_patterns.get(pattern_key, []) or config_patterns.get(f"{pattern_key}_patterns", [])
        for pattern_config in patterns:
            pattern = pattern_config.get("pattern", "")
            name = pattern_config.get("name", "")
            if not pattern or not name:
                continue

            try:
                # Find matches in full text
                for match in re.finditer(pattern, full_text, re.IGNORECASE):
                    value = match.group(1) if match.groups() else match.group(0)

                    # Find line number
                    pos = match.start()
                    line_num = full_text[:pos].count("\n") + 1

                    # Get context (the matched line)
                    context = ""
                    if 0 < line_num <= len(lines):
                        context = lines[line_num - 1].strip()

                    # Only keep first occurrence of each config
                    if name not in config_dict:
                        config_dict[name] = TrackedConfig(
                            name=name,
                            value=value,
                            layer=layer,
                            source_line=line_num,
                            raw_context=context[:200],  # Truncate context
                        )
            except re.error:
                # Skip invalid regex patterns
                continue

    return layer_configs


def correlate_error_with_configs(
    error_text: str,
    layer_configs: LayerConfigs,
    error_layer: ConfigLayer,
    config_patterns: dict | None = None,
) -> list[ConfigAttribution]:
    """
    Correlate error messages with configurations that may have caused them.

    Args:
        error_text: The error message or error section text
        layer_configs: Extracted configurations from each layer
        error_layer: The layer where the error occurred
        config_patterns: Patterns including error_config_mappings

    Returns:
        List of ConfigAttribution objects for each correlation found
    """
    if config_patterns is None:
        config_patterns = {}

    attributions = []
    error_mappings = config_patterns.get("error_config_mappings", [])

    for mapping in error_mappings:
        error_pattern = mapping.get("error_pattern", "")
        related_configs = mapping.get("related_configs", [])
        explanation = mapping.get("explanation", "")
        suggested_fix = mapping.get("suggested_fix", "")
        mapping_error_layer = mapping.get("error_layer", "framework")

        if not error_pattern:
            continue

        try:
            if re.search(error_pattern, error_text, re.IGNORECASE):
                # Found a matching error pattern - look for related configs
                for config_name in related_configs:
                    config = layer_configs.get_config(config_name)
                    if config:
                        # Determine if this is a higher-layer cause
                        config_layer = config.layer
                        err_layer = ConfigLayer.from_string(mapping_error_layer)

                        attribution = ConfigAttribution(
                            error_param_name=config_name,
                            error_layer=err_layer,
                            source_config=config,
                            source_layer=config_layer,
                            explanation=explanation,
                            suggested_fix=suggested_fix,
                        )
                        attributions.append(attribution)
        except re.error:
            continue

    return attributions


def format_extracted_log(extracted: ExtractedLog) -> str:
    """Format extracted log for LLM consumption."""
    parts = []

    parts.append("=" * 60)
    parts.append("JOB CONTEXT")
    parts.append("=" * 60)
    parts.append(extracted.job_info or "(no job context found)")

    # Include layer configs if available
    if extracted.layer_configs:
        lc = extracted.layer_configs
        if any([lc.application, lc.serving, lc.model, lc.framework]):
            parts.append("\n" + "=" * 60)
            parts.append("EXTRACTED CONFIGURATIONS BY LAYER")
            parts.append("=" * 60)
            if lc.application:
                parts.append("\n[Application Layer]")
                for name, cfg in list(lc.application.items())[:5]:
                    parts.append(f"  {name}: {cfg.value}")
            if lc.serving:
                parts.append("\n[Serving Layer]")
                for name, cfg in list(lc.serving.items())[:8]:
                    parts.append(f"  {name}: {cfg.value}")
            if lc.model:
                parts.append("\n[Model Layer]")
                for name, cfg in list(lc.model.items())[:5]:
                    parts.append(f"  {name}: {cfg.value}")
            if lc.framework:
                parts.append("\n[Framework Layer]")
                for name, cfg in list(lc.framework.items())[:5]:
                    parts.append(f"  {name}: {cfg.value}")

    parts.append("\n" + "=" * 60)
    parts.append(f"ERROR SECTIONS ({len(extracted.error_sections)} found)")
    parts.append("=" * 60)
    for i, section in enumerate(extracted.error_sections, 1):
        parts.append(f"\n--- Error Section {i} ---")
        parts.append(section)

    if extracted.deduplicated_warnings:
        parts.append("\n" + "=" * 60)
        parts.append("DEDUPLICATED WARNINGS (showing count)")
        parts.append("=" * 60)
        for warning, count in extracted.deduplicated_warnings.items():
            parts.append(f"  [{count}x] {warning[:100]}...")

    # Show failed tests if any
    if extracted.failed_tests:
        parts.append("\n" + "=" * 60)
        parts.append(f"FAILED TESTS ({len(extracted.failed_tests)} found)")
        parts.append("=" * 60)
        for test in extracted.failed_tests[:15]:
            parts.append(f"  - {test}")
        if len(extracted.failed_tests) > 15:
            parts.append(f"  ... and {len(extracted.failed_tests) - 15} more")

    # Show failed evals if any (accuracy below target)
    if extracted.failed_evals:
        parts.append("\n" + "=" * 60)
        parts.append(f"FAILED EVALS - ACCURACY BELOW TARGET ({len(extracted.failed_evals)} found)")
        parts.append("=" * 60)
        parts.append("These benchmarks completed but scored below the target threshold:")
        for eval_name in extracted.failed_evals[:15]:
            parts.append(f"  - {eval_name}")
        if len(extracted.failed_evals) > 15:
            parts.append(f"  ... and {len(extracted.failed_evals) - 15} more")

    parts.append("\n" + "=" * 60)
    parts.append("FINAL STATUS")
    parts.append("=" * 60)
    parts.append(extracted.final_status or "(no final status found)")

    # Show outcome summary
    if extracted.has_crash:
        parts.append("\n[OUTCOME: CRASHED - job did not complete]")
    elif extracted.has_timeout:
        parts.append("\n[OUTCOME: TIMEOUT - job did not complete]")
    elif extracted.failed_tests:
        parts.append(f"\n[OUTCOME: TESTS FAILED - {len(extracted.failed_tests)} test(s) failed]")
    elif extracted.failed_evals:
        parts.append(f"\n[OUTCOME: EVALS BELOW TARGET - {len(extracted.failed_evals)} eval(s) below threshold]")
    elif extracted.exit_code is not None and extracted.exit_code != 0:
        parts.append(f"\n[OUTCOME: FAILED - exit code {extracted.exit_code}]")
    else:
        parts.append("\n[OUTCOME: SUCCESS]")

    parts.append(f"[Extracted {extracted.extracted_lines}/{extracted.total_lines} lines]")

    return "\n".join(parts)


def calculate_time_after_error(
    error_message: str,
    extracted_log: ExtractedLog,
) -> None:
    """
    Calculate how long the job ran after the LLM-identified error occurred.

    This is a second-pass function called after the LLM identifies the primary error.
    It searches for that error in the log, finds its timestamp, and calculates
    the time from there until job end.

    Args:
        error_message: The error message identified by the LLM
        extracted_log: The ExtractedLog object to update with timing info (uses raw_lines)
    """
    if not error_message or not error_message.strip():
        return

    lines = extracted_log.raw_lines
    if not lines:
        return

    # Search for the error message in the log
    # Use a simplified/normalized search to handle minor differences
    error_lower = error_message.lower().strip()
    error_line_idx = None

    for i, line in enumerate(lines):
        if error_lower in line.lower():
            error_line_idx = i
            break

    if error_line_idx is None:
        # Try partial match - first 50 chars of error
        error_prefix = error_lower[:50] if len(error_lower) > 50 else error_lower
        for i, line in enumerate(lines):
            if error_prefix in line.lower():
                error_line_idx = i
                break

    if error_line_idx is None:
        return  # Couldn't find the error in the log

    # Get timestamp of the error line
    error_ts = parse_timestamp(lines[error_line_idx])
    if not error_ts:
        # Try nearby lines (error might be on a line without timestamp)
        for offset in range(-3, 4):
            idx = error_line_idx + offset
            if 0 <= idx < len(lines):
                error_ts = parse_timestamp(lines[idx])
                if error_ts:
                    break

    if not error_ts:
        return  # No timestamp found

    extracted_log.crash_timestamp = error_ts.isoformat()

    # Find job end timestamp (last line with a timestamp)
    job_end_ts = None
    for line in reversed(lines[-100:]):
        job_end_ts = parse_timestamp(line)
        if job_end_ts:
            extracted_log.job_end_timestamp = job_end_ts.isoformat()
            break

    # Calculate duration after error
    if job_end_ts and error_ts:
        delta = job_end_ts - error_ts
        if delta.total_seconds() > 0:  # Only if job continued after error
            extracted_log.time_after_crash_seconds = int(delta.total_seconds())
