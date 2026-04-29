# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Config context extraction - find configuration examples and usage patterns.

This module provides context to the LLM about how configuration parameters
are used, by:
1. Extracting config examples from the log itself
2. Searching the codebase for how parameters are configured
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# TT-specific CLI parameter keywords — used by extract_config_examples_from_log
# to filter CLI args down to TT-relevant parameters.
_TT_CLI_KEYWORDS = ["trace", "l1", "worker", "fabric", "metal", "tt_"]


@dataclass
class ConfigExample:
    """A configuration example found in logs or code."""

    param_name: str  # e.g., "trace_region_size"
    value: str  # e.g., "51934848"
    source: str  # e.g., "override_tt_config", "CLI arg", "env var"
    context: str  # Surrounding context showing how it's used
    file_path: str = ""  # If from codebase


@dataclass
class ConfigContext:
    """Configuration context for the LLM."""

    # Examples found in the log
    log_examples: dict[str, list[ConfigExample]] = field(default_factory=dict)

    # Examples found in codebase
    code_examples: dict[str, list[ConfigExample]] = field(default_factory=dict)

    # Parameters mentioned in errors (what we're looking for context on)
    error_params: list[str] = field(default_factory=list)


def extract_config_examples_from_log(log_content: str) -> dict[str, list[ConfigExample]]:
    """
    Extract configuration examples from log content.

    Finds patterns like:
    - override_tt_config: {...}
    - non-default args: {...}
    - --parameter value

    Returns dict mapping parameter names to examples of how they're configured.
    """
    examples: dict[str, list[ConfigExample]] = {}

    # Pattern 1: non-default args (vLLM logs this on startup)
    # Example: non-default args: {'model': '...', 'override_tt_config': {...}, ...}
    # Find the start of the pattern and then extract balanced braces
    for match in re.finditer(r"non-default args:\s*\{", log_content):
        start_pos = match.end() - 1  # Position of opening brace
        args_str = _extract_balanced_dict(log_content[start_pos:])
        if args_str:
            try:
                # Convert Python dict syntax to JSON
                json_str = (
                    args_str.replace("'", '"').replace("True", "true").replace("False", "false").replace("None", "null")
                )
                args = json.loads(json_str)
                _add_config_examples(examples, args, "vLLM non-default args", args_str[:200])
            except (json.JSONDecodeError, ValueError):
                # Malformed non-default args in logs are expected; skip this snippet
                pass

    # Pattern 2: override_tt_config as JSON string
    # Example: "override_tt_config": "{\"trace_region_size\": 51934848, ...}"
    # Cap at 2000 chars to prevent ReDoS: [^"]+ backtracks catastrophically on long
    # lines when no closing }" is found. Real override_tt_config values are <300 chars.
    override_json_pattern = r'"override_tt_config":\s*"(\{[^"]{0,2000}\})"'
    for match in re.finditer(override_json_pattern, log_content):
        try:
            config_str = match.group(1).replace('\\"', '"')
            config = json.loads(config_str)
            _add_config_examples(examples, config, "override_tt_config (JSON)", match.group(0)[:200])
        except json.JSONDecodeError:
            pass

    # Pattern 3: override_tt_config as Python dict — use _extract_balanced_dict
    # instead of [^}]+ which catastrophically backtracks on long lines: the engine
    # retries at every split point when \} isn't found, O(n²) on lines with many braces.
    # _extract_balanced_dict is O(n) with no backtracking and handles nested dicts.
    for match in re.finditer(r"'override_tt_config':\s*(\{)", log_content):
        start_pos = match.start(1)
        dict_str = _extract_balanced_dict(log_content[start_pos:])
        if dict_str:
            try:
                # Convert Python dict syntax to JSON (same chain as Pattern 1)
                json_str = (
                    dict_str.replace("'", '"').replace("True", "true").replace("False", "false").replace("None", "null")
                )
                config = json.loads(json_str)
                _add_config_examples(examples, config, "override_tt_config (dict)", dict_str[:200])
            except json.JSONDecodeError:
                # Single-quote → double-quote substitution is lossy; malformed results are expected
                pass

    # Pattern 4: CLI arguments like --trace_region_size 51934848
    cli_pattern = r"--(\w+)\s+(\d+|true|false|[\w/.-]+)"
    for match in re.finditer(cli_pattern, log_content, re.IGNORECASE):
        param = match.group(1)
        value = match.group(2)
        # Only include TT-related params
        if any(kw in param.lower() for kw in _TT_CLI_KEYWORDS):
            if param not in examples:
                examples[param] = []
            examples[param].append(
                ConfigExample(
                    param_name=param,
                    value=value,
                    source="CLI argument",
                    context=match.group(0),
                )
            )

    return examples


def _extract_balanced_dict(s: str, max_len: int = 10000) -> str | None:
    """Extract a balanced dict from a string (handle nested braces).

    Uses an O(n) character scan instead of regex to avoid catastrophic backtracking
    on strings with many `}` characters. Returns None if the dict exceeds max_len
    characters or if input is truncated before the dict closes.
    """
    depth = 0
    start = None
    for i, c in enumerate(s):
        if i > max_len:
            return None
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return s[start : i + 1]
    return None


def _add_config_examples(
    examples: dict[str, list[ConfigExample]],
    config: dict,
    source: str,
    context: str,
) -> None:
    """Add config parameters to examples dict."""
    for key, value in config.items():
        if isinstance(value, dict):
            # Nested config (like override_tt_config)
            for nested_key, nested_value in value.items():
                if nested_key not in examples:
                    examples[nested_key] = []
                # Deduplicate by value
                if not any(e.value == str(nested_value) for e in examples[nested_key]):
                    examples[nested_key].append(
                        ConfigExample(
                            param_name=nested_key,
                            value=str(nested_value),
                            source=source,
                            context=context,
                        )
                    )
        else:
            if key not in examples:
                examples[key] = []
            if not any(e.value == str(value) for e in examples[key]):
                examples[key].append(
                    ConfigExample(
                        param_name=key,
                        value=str(value),
                        source=source,
                        context=context,
                    )
                )


def extract_error_params(error_content: str) -> list[str]:
    """
    Extract parameter names mentioned in error messages.

    These are the parameters we want to find configuration context for.
    """
    params = set()

    # Common TT-Metal/vLLM config parameters that might appear in errors
    known_params = [
        "trace_region_size",
        "l1_small_size",
        "worker_l1_size",
        "fabric_config",
        "max_model_len",
        "max_num_seqs",
        "block_size",
        "dispatch_timeout",
        "mesh_device",
    ]

    for param in known_params:
        if param in error_content.lower():
            params.add(param)

    # Also look for patterns like "parameter_name <= value" or "parameter_name: value"
    # Use [a-zA-Z0-9] (not \w which includes _) to keep _ as an unambiguous separator
    # and avoid catastrophic backtracking on strings with many underscores.
    param_pattern = r"([a-zA-Z][a-zA-Z0-9]*(?:_[a-zA-Z0-9]+)+)\s*(?:<=|>=|<|>|==|!=|:)\s*\d+"
    for match in re.finditer(param_pattern, error_content):
        param = match.group(1)
        if len(param) > 3:  # Skip very short matches
            params.add(param)

    return list(params)


def search_codebase_for_config(
    param_name: str,
    repo_paths: dict[str, str],
    max_results: int = 3,
) -> list[ConfigExample]:
    """
    Search the codebase for how a parameter is configured.

    Searches for:
    - Config files (YAML, JSON, TOML)
    - Python files setting the parameter
    - Documentation mentioning the parameter
    """
    examples = []

    for repo_name, repo_path in repo_paths.items():
        if not Path(repo_path).exists():
            continue

        # Use grep to find files mentioning the parameter
        try:
            result = subprocess.run(
                [
                    "grep",
                    "-r",
                    "-l",
                    "--include=*.yaml",
                    "--include=*.yml",
                    "--include=*.json",
                    "--include=*.py",
                    "--include=*.md",
                    param_name,
                    repo_path,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                files = result.stdout.strip().split("\n")[: max_results * 2]

                for file_path in files:
                    if not file_path:
                        continue

                    # Skip test files and generated files
                    if any(
                        skip in file_path
                        for skip in [
                            "/test",
                            "/__pycache__",
                            "/.git",
                            "/node_modules",
                            "/build/",
                            "/dist/",
                            ".egg-info",
                        ]
                    ):
                        continue

                    # Get context around the parameter usage
                    context = _get_file_context(file_path, param_name)
                    if context:
                        # Determine source type
                        if file_path.endswith((".yaml", ".yml")):
                            source = "YAML config"
                        elif file_path.endswith(".json"):
                            source = "JSON config"
                        elif file_path.endswith(".py"):
                            source = "Python code"
                        elif file_path.endswith(".md"):
                            source = "Documentation"
                        else:
                            source = "Config file"

                        # Make path relative to repo
                        rel_path = file_path.replace(repo_path, "").lstrip("/")

                        examples.append(
                            ConfigExample(
                                param_name=param_name,
                                value="(see context)",
                                source=f"{source} [{repo_name}]",
                                context=context,
                                file_path=f"{repo_name}/{rel_path}",
                            )
                        )

                        if len(examples) >= max_results:
                            return examples

        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            # Grep search failed or timed out; continue with other repos
            pass

    return examples


def _get_file_context(file_path: str, param_name: str, context_lines: int = 3) -> str | None:
    """Get context around parameter usage in a file."""
    try:
        result = subprocess.run(
            ["grep", "-n", "-B", str(context_lines), "-A", str(context_lines), param_name, file_path],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0 and result.stdout.strip():
            # Limit context size
            lines = result.stdout.strip().split("\n")[:10]
            return "\n".join(lines)

    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        # Grep failed or timed out; return None to indicate no context found
        pass

    return None


def search_sibling_logs_for_config(
    log_path: Path | str,
    param_name: str,
    max_files: int = 5,
) -> list[ConfigExample]:
    """
    Search sibling log files for configuration examples.

    When analyzing a failing log, other logs in the same directory might
    have working configurations that show how to set the parameter.
    """
    examples = []
    log_path = Path(log_path)

    if not log_path.exists():
        return examples

    # Get the directory containing the log
    log_dir = log_path.parent if log_path.is_file() else log_path

    # Find other log files
    log_files = list(log_dir.glob("*.log"))[: max_files * 2]

    for other_log in log_files:
        if other_log == log_path:
            continue

        try:
            content = other_log.read_text(errors="replace")
            other_examples = extract_config_examples_from_log(content)

            if param_name in other_examples:
                for ex in other_examples[param_name]:
                    ex.file_path = other_log.name
                    ex.source = f"{ex.source} (from {other_log.name})"
                    examples.append(ex)

                    if len(examples) >= max_files:
                        return examples

        except (IOError, UnicodeDecodeError):
            continue

    return examples


def gather_config_context(
    log_content: str,
    error_content: str,
    repo_paths: dict[str, str] | None = None,
    log_path: Path | str | None = None,
) -> ConfigContext:
    """
    Gather configuration context for the LLM.

    Args:
        log_content: Full log content
        error_content: Error sections from the log
        repo_paths: Dict mapping repo names to local paths
        log_path: Path to the log file (for searching sibling logs)

    Returns:
        ConfigContext with examples from log and codebase
    """
    ctx = ConfigContext()

    # Extract parameters mentioned in errors
    ctx.error_params = extract_error_params(error_content)

    # Extract config examples from the log
    ctx.log_examples = extract_config_examples_from_log(log_content)

    # For params in errors that aren't in this log's config, search sibling logs
    if log_path and ctx.error_params:
        for param in ctx.error_params:
            if param not in ctx.log_examples:
                sibling_examples = search_sibling_logs_for_config(log_path, param)
                if sibling_examples:
                    ctx.log_examples[param] = sibling_examples

    # Search codebase for parameters mentioned in errors
    if repo_paths and ctx.error_params:
        for param in ctx.error_params:
            code_examples = search_codebase_for_config(param, repo_paths)
            if code_examples:
                ctx.code_examples[param] = code_examples

    return ctx


def format_config_context_for_prompt(ctx: ConfigContext) -> str:
    """Format configuration context for the LLM prompt."""
    if not ctx.log_examples and not ctx.code_examples:
        return ""

    parts = []
    parts.append("=" * 60)
    parts.append("CONFIGURATION CONTEXT")
    parts.append("=" * 60)
    parts.append("Use this context to provide accurate configuration advice.\n")

    # Show parameters found in errors
    if ctx.error_params:
        parts.append(f"**Parameters in error:** {', '.join(ctx.error_params)}\n")

    # Show examples from the log
    relevant_log_examples = {}
    for param in ctx.error_params:
        if param in ctx.log_examples:
            relevant_log_examples[param] = ctx.log_examples[param]

    if relevant_log_examples:
        parts.append("### Configuration Examples from This Log")
        parts.append("These show how the parameter IS configured in working runs:\n")

        for param, examples in relevant_log_examples.items():
            for ex in examples[:2]:  # Limit to 2 per param
                parts.append(f"**{param}** = `{ex.value}`")
                parts.append(f"  Source: {ex.source}")
                if ex.context and len(ex.context) < 150:
                    parts.append(f"  Context: `{ex.context}`")
                parts.append("")

    # Show examples from codebase
    if ctx.code_examples:
        parts.append("### Configuration Usage from Codebase")
        parts.append("These show how the parameter is used in code/configs:\n")

        for param, examples in ctx.code_examples.items():
            for ex in examples[:2]:
                parts.append(f"**{param}** in `{ex.file_path}`")
                parts.append(f"  Source: {ex.source}")
                parts.append("```")
                parts.append(ex.context[:500] if ex.context else "(no context)")
                parts.append("```")
                parts.append("")

    return "\n".join(parts)
