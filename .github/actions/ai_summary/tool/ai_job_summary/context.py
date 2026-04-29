# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Context gathering - collect information about the CI run.

Gathers:
- PR changed files (from git/gh)
- CODEOWNERS mappings
- Test YAML definitions
- Job metadata
- Code context from stack traces (multi-repo support)
"""

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# Default repo path mappings - can be overridden via env vars or config
# Maps path prefixes to repo names for identification
DEFAULT_PATH_PREFIXES = {
    "tt_metal/": "tt-metal",
    "ttnn/": "tt-metal",
    "tt-metal/": "tt-metal",
    "vllm/": "vllm",
    "tt_inference_server/": "tt-inference-server",
    "inference_server/": "tt-inference-server",
}


@dataclass
class PRContext:
    """Context from the PR/branch."""

    branch: str = ""
    pr_number: str = ""
    changed_files: list[str] = field(default_factory=list)
    base_branch: str = "main"


@dataclass
class JobContext:
    """Context from test YAML definitions."""

    job_name: str = ""
    cmd: str = ""
    owner_id: str = ""
    owner_name: str = ""
    team: str = ""
    timeout_minutes: int = 0


@dataclass
class CodeOwnership:
    """Code ownership information."""

    # file path -> list of owners
    owners: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class FileSnippet:
    """A code snippet from a file in the stack trace."""

    file_path: str  # Original path from stack trace
    repo_name: str  # Inferred repo name (e.g., "tt-metal")
    line_number: int  # Line number from stack trace
    content: str  # Code snippet with context
    local_path: str = ""  # Actual path where file was found
    function_name: str = ""  # Name of the function containing this line
    is_full_function: bool = False  # True if content is the complete function
    stack_depth: int = 0  # Position in call stack (0 = deepest/error location)


@dataclass
class CodeContext:
    """Code context extracted from stack traces."""

    snippets: list[FileSnippet] = field(default_factory=list)
    repo_paths: dict[str, str] = field(default_factory=dict)  # repo_name -> local path


@dataclass
class CIContext:
    """Combined CI context."""

    pr: PRContext = field(default_factory=PRContext)
    job: JobContext = field(default_factory=JobContext)
    codeowners: CodeOwnership = field(default_factory=CodeOwnership)
    repo_root: Path = field(default_factory=Path)
    code: CodeContext = field(default_factory=CodeContext)  # Code from stack traces


def get_pr_context(repo_path: Path, pr_number: str = None) -> PRContext:
    """
    Get PR context from git/GitHub CLI.

    Args:
        repo_path: Path to the repository
        pr_number: Optional PR number (if not provided, uses current branch)
    """
    ctx = PRContext()

    try:
        # Get current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        ctx.branch = result.stdout.strip()

        # Get changed files
        if pr_number:
            # Use gh CLI to get PR files
            result = subprocess.run(
                ["gh", "pr", "view", pr_number, "--json", "files", "-q", ".files[].path"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                ctx.changed_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
                ctx.pr_number = pr_number
        else:
            # Get diff against main
            result = subprocess.run(
                ["git", "diff", "main...HEAD", "--name-only"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                ctx.changed_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]

    except Exception as e:
        print(f"Warning: Could not get PR context: {e}")

    return ctx


def parse_codeowners(codeowners_path: Path) -> CodeOwnership:
    """
    Parse a CODEOWNERS file.

    Returns mapping of path patterns to owners.
    """
    ownership = CodeOwnership()

    if not codeowners_path.exists():
        return ownership

    with open(codeowners_path) as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if len(parts) >= 2:
                path_pattern = parts[0]
                owners = parts[1:]
                ownership.owners[path_pattern] = owners

    return ownership


def find_owners_for_file(file_path: str, codeowners: CodeOwnership) -> list[str]:
    """Find CODEOWNERS for a specific file path."""
    matched_owners = []

    for pattern, owners in codeowners.owners.items():
        # Convert CODEOWNERS glob to regex (simplified)
        regex_pattern = pattern.replace("*", ".*").replace("?", ".")
        if pattern.startswith("/"):
            regex_pattern = "^" + regex_pattern[1:]

        if re.search(regex_pattern, file_path):
            matched_owners.extend(owners)

    return list(set(matched_owners))


def load_test_yaml(yaml_path: Path) -> list[JobContext]:
    """Load test definitions from YAML."""
    jobs = []

    if not yaml_path.exists():
        return jobs

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, list):
        return jobs

    for item in data:
        job = JobContext(
            job_name=item.get("name", ""),
            cmd=item.get("cmd", ""),
            owner_id=item.get("owner_id", ""),
            team=item.get("team", ""),
            timeout_minutes=item.get("timeout", 0),
        )
        # Extract owner name from comment if present
        # Format: "owner_id: UXXXX # Name"
        jobs.append(job)

    return jobs


def find_job_by_name(jobs: list[JobContext], job_name: str) -> JobContext | None:
    """Find a job definition by name."""
    for job in jobs:
        if job.job_name == job_name:
            return job
    return None


def extract_job_name_from_log(log_content: str) -> str | None:
    """Extract job name from log content."""
    # Look for "Complete job name:" pattern
    match = re.search(r"Complete job name:\s*(.+?)(?:\n|$)", log_content)
    if match:
        # Format: "parent / child" - we want the child
        full_name = match.group(1).strip()
        if "/" in full_name:
            return full_name.split("/")[-1].strip()
        return full_name
    return None


def gather_context(
    repo_path: Path,
    log_content: str,
    test_yaml_path: Path = None,
    pr_number: str = None,
    include_code_context: bool = True,
) -> CIContext:
    """
    Gather all context for CI summarization.

    Args:
        repo_path: Path to the repository root
        log_content: Content of the log file (or first part of it)
        test_yaml_path: Path to test definitions YAML
        pr_number: Optional PR number
        include_code_context: Whether to extract code from stack traces
    """
    ctx = CIContext(repo_root=repo_path)

    # Get PR context
    ctx.pr = get_pr_context(repo_path, pr_number)

    # Load CODEOWNERS
    codeowners_path = repo_path / ".github" / "CODEOWNERS"
    if not codeowners_path.exists():
        codeowners_path = repo_path / "CODEOWNERS"
    ctx.codeowners = parse_codeowners(codeowners_path)

    # Load test YAML and find matching job
    if test_yaml_path and test_yaml_path.exists():
        jobs = load_test_yaml(test_yaml_path)
        job_name = extract_job_name_from_log(log_content)
        if job_name:
            job = find_job_by_name(jobs, job_name)
            if job:
                ctx.job = job

    # Gather code context from stack traces (auto-discovers repo paths from log)
    if include_code_context:
        ctx.code = gather_code_context(log_content)

    return ctx


def discover_repo_paths_from_log(log_content: str) -> dict[str, str]:
    """
    Discover repo paths by analyzing absolute paths in the log/stack trace.

    Extracts paths like:
        /home/runner/work/tt-inference-server/tt-metal/tt_metal/impl/foo.cpp
    And infers:
        tt-metal -> /home/runner/work/tt-inference-server/tt-metal
    """
    paths = {}

    # Known repo directory names to look for
    known_repos = {
        "tt-metal": "tt-metal",
        "tt_metal": "tt-metal",  # Sometimes used without hyphen
        "vllm": "vllm",
        "tt-inference-server": "tt-inference-server",
        "tt_inference_server": "tt-inference-server",
    }

    # Find absolute paths in the log
    # Match paths like /home/runner/work/repo/subdir/file.ext
    path_pattern = r"(/(?:home|work|opt|tmp|var|github)[/\w.-]+?)/(tt[-_]metal|vllm|tt[-_]inference[-_]server)/([/\w.-]+\.(?:py|cpp|cc|c|h|hpp|rs))"

    for match in re.finditer(path_pattern, log_content, re.IGNORECASE):
        prefix = match.group(1)
        repo_dir = match.group(2)

        # Normalize repo name
        repo_name = known_repos.get(repo_dir.lower().replace("_", "-"), repo_dir)

        if repo_name in paths:
            continue

        # Construct the full repo path
        repo_path = f"{prefix}/{repo_dir}"

        # Verify it exists (if we're on the same machine)
        if Path(repo_path).exists():
            paths[repo_name] = repo_path

    return paths


def discover_repo_paths(base_dir: Path | None = None, log_content: str | None = None) -> dict[str, str]:
    """
    Discover repo paths automatically.

    Priority:
    1. Paths extracted from log content (most reliable on CI runners)
    2. Environment variables
    3. Sibling directories of base_dir
    4. Common CI workspace layouts
    """
    paths = {}

    # 1. Extract from log content (highest priority - these are the actual paths used)
    if log_content:
        paths.update(discover_repo_paths_from_log(log_content))

    # 2. Check explicit env vars
    env_mappings = {
        "TT_METAL_PATH": "tt-metal",
        "VLLM_PATH": "vllm",
        "TT_INFERENCE_SERVER_PATH": "tt-inference-server",
    }

    for env_var, repo_name in env_mappings.items():
        if repo_name in paths:
            continue
        if path := os.environ.get(env_var):
            if Path(path).exists():
                paths[repo_name] = path

    # 3. Check for JSON-formatted REPO_PATHS
    if repo_paths_json := os.environ.get("REPO_PATHS"):
        try:
            extra_paths = json.loads(repo_paths_json)
            for repo_name, path in extra_paths.items():
                if repo_name not in paths and Path(path).exists():
                    paths[repo_name] = path
        except json.JSONDecodeError:
            # Invalid JSON in REPO_PATHS env var - silently ignore and fall back to auto-discovery
            pass

    # 4. Try to discover from common locations
    search_dirs = []

    if base_dir:
        search_dirs.append(base_dir.parent)
        search_dirs.append(base_dir)

    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        search_dirs.append(Path(workspace))
        search_dirs.append(Path(workspace).parent)

    known_repos = ["tt-metal", "vllm", "tt-inference-server"]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for repo_name in known_repos:
            if repo_name in paths:
                continue
            repo_path = search_dir / repo_name
            if repo_path.exists() and (repo_path / ".git").exists():
                paths[repo_name] = str(repo_path)

    return paths


def extract_files_from_stack(log_content: str) -> list[tuple[str, int]]:
    """
    Extract file paths and line numbers from stack traces in log content.

    Returns list of (file_path, line_number) tuples.
    """
    files = []
    seen = set()

    # Patterns for different stack trace formats
    patterns = [
        # C++: /path/to/file.cpp:123 or file.cpp:123:45
        r"([/\w.-]+\.(?:cpp|cc|c|h|hpp|cxx)):(\d+)",
        # Python: File "/path/to/file.py", line 45
        r'File "([^"]+\.py)", line (\d+)',
        # Python traceback: /path/to/file.py:123
        r"([/\w.-]+\.py):(\d+)",
        # Rust: at /path/to/file.rs:123
        r"at\s+([/\w.-]+\.rs):(\d+)",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, log_content):
            file_path = match.group(1)
            line_num = int(match.group(2))

            # Skip if already seen this file
            if file_path in seen:
                continue
            seen.add(file_path)

            # Skip standard library / system paths
            skip_prefixes = [
                "/usr/",
                "/lib/",
                "/opt/",
                "/nix/",
                "site-packages",
                "dist-packages",
                "<frozen",
                "<string>",
                "<module>",
            ]
            if any(skip in file_path for skip in skip_prefixes):
                continue

            files.append((file_path, line_num))

    return files


def infer_repo_from_path(file_path: str) -> str | None:
    """Infer which repo a file belongs to based on path patterns."""
    for prefix, repo_name in DEFAULT_PATH_PREFIXES.items():
        if prefix in file_path:
            return repo_name
    return None


def normalize_file_path(file_path: str) -> str:
    """
    Normalize a file path for lookup in a repo.

    Strips absolute path components to get the repo-relative path.
    E.g., /home/runner/work/tt-metal/tt_metal/foo.cpp -> tt_metal/foo.cpp
    """
    # Find the first known prefix and return from there
    for prefix in DEFAULT_PATH_PREFIXES:
        if prefix in file_path:
            idx = file_path.index(prefix)
            return file_path[idx:]

    # If no prefix found, try to strip common CI paths
    ci_markers = ["/work/", "/runner/", "/actions-runner/", "/home/"]
    for marker in ci_markers:
        if marker in file_path:
            # Take the last component after splitting by the marker
            parts = file_path.split(marker)
            if len(parts) > 1:
                # Remove the first directory (usually repo name)
                remaining = parts[-1]
                slash_idx = remaining.find("/")
                if slash_idx > 0:
                    return remaining[slash_idx + 1 :]

    return file_path


def extract_python_function(content: str, line_number: int) -> tuple[str, str, int, int] | None:
    """
    Extract the full function/method containing the given line using AST.

    Args:
        content: Full file content
        line_number: Line number (1-indexed) to find function for

    Returns:
        Tuple of (function_name, function_source, start_line, end_line) or None
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None

    lines = content.split("\n")

    # Find all functions/methods and their line ranges
    functions = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Get the end line (Python 3.8+)
            end_line = getattr(node, "end_lineno", node.lineno + 10)
            functions.append((node.name, node.lineno, end_line))

    # Find the function containing our line
    for func_name, start, end in functions:
        if start <= line_number <= end:
            # Extract the function source with line numbers
            func_lines = []
            for i in range(start - 1, min(end, len(lines))):
                marker = ">>>" if i + 1 == line_number else "   "
                func_lines.append(f"{marker} {i+1:4d}: {lines[i]}")
            return (func_name, "\n".join(func_lines), start, end)

    return None


def extract_cpp_function(content: str, line_number: int) -> tuple[str, str, int, int] | None:
    """
    Extract a C++ function containing the given line using heuristics.

    Uses brace matching to find function boundaries.

    Args:
        content: Full file content
        line_number: Line number (1-indexed) to find function for

    Returns:
        Tuple of (function_name, function_source, start_line, end_line) or None
    """
    lines = content.split("\n")
    if line_number > len(lines):
        return None

    # Search backwards for function start (line with opening brace at end or function signature)
    func_start = None
    func_name = "unknown"

    # C++ keywords that are NOT function names
    cpp_keywords = {
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "try",
        "catch",
        "return",
        "throw",
        "new",
        "delete",
        "sizeof",
        "typeof",
        "namespace",
    }

    # Pattern for function definition - requires return type before name
    # Matches: void foo(), int bar(int x), std::string baz() const
    func_pattern = re.compile(
        r"^\s*"  # Leading whitespace
        r"(?:(?:static|virtual|inline|explicit|constexpr|const|volatile|unsigned|signed)\s+)*"  # Optional specifiers
        r"(?:[\w:*&<>]+\s+)+"  # Return type (required, with namespace/template support)
        r"(\w+)\s*"  # Function name (captured)
        r"\([^)]*\)\s*"  # Parameters
        r"(?:const|override|noexcept|final|\s)*"  # Optional trailing specifiers
        r"\{?\s*$"  # Optional opening brace
    )

    for i in range(line_number - 1, max(0, line_number - 100), -1):
        line = lines[i]
        # Look for function signature
        match = func_pattern.match(line)
        if match:
            name = match.group(1)
            if name not in cpp_keywords:
                func_name = name
                func_start = i
                break
        # Also check for standalone opening brace after signature
        if line.strip() == "{" and i > 0:
            prev_line = lines[i - 1]
            match = func_pattern.match(prev_line)
            if match:
                name = match.group(1)
                if name not in cpp_keywords:
                    func_name = name
                    func_start = i - 1
                    break

    if func_start is None:
        # Fallback: just take context around the line
        return None

    # Find function end by matching braces
    brace_count = 0
    func_end = func_start
    started = False

    for i in range(func_start, min(len(lines), func_start + 500)):
        line = lines[i]
        for char in line:
            if char == "{":
                brace_count += 1
                started = True
            elif char == "}":
                brace_count -= 1

        if started and brace_count == 0:
            func_end = i
            break

    # Extract with line numbers
    func_lines = []
    for i in range(func_start, min(func_end + 1, len(lines))):
        marker = ">>>" if i + 1 == line_number else "   "
        func_lines.append(f"{marker} {i+1:4d}: {lines[i]}")

    return (func_name, "\n".join(func_lines), func_start + 1, func_end + 1)


def extract_full_function(
    file_path: Path,
    line_number: int,
    max_lines: int = 100,
) -> tuple[str, str, bool] | None:
    """
    Extract the full function containing the given line.

    Args:
        file_path: Path to the source file
        line_number: Line number (1-indexed)
        max_lines: Maximum lines to include (truncate if larger)

    Returns:
        Tuple of (function_name, formatted_content, is_full_function) or None
    """
    try:
        content = file_path.read_text(errors="replace")
    except (IOError, UnicodeDecodeError):
        return None

    lines = content.split("\n")
    suffix = file_path.suffix.lower()

    result = None
    if suffix == ".py":
        result = extract_python_function(content, line_number)
    elif suffix in (".cpp", ".cc", ".c", ".h", ".hpp", ".cxx"):
        result = extract_cpp_function(content, line_number)

    if result:
        func_name, func_content, start, end = result
        func_lines = func_content.split("\n")

        # Truncate if too long
        if len(func_lines) > max_lines:
            # Keep start and end, truncate middle
            keep_start = max_lines // 2
            keep_end = max_lines - keep_start - 1
            func_lines = (
                func_lines[:keep_start]
                + [f"       ... ({len(func_lines) - max_lines} lines truncated) ..."]
                + func_lines[-keep_end:]
            )
            return (func_name, "\n".join(func_lines), False)

        return (func_name, func_content, True)

    # Fallback: return context around the line
    context = 15
    start = max(0, line_number - context - 1)
    end = min(len(lines), line_number + context)

    snippet_lines = []
    for i in range(start, end):
        marker = ">>>" if i + 1 == line_number else "   "
        snippet_lines.append(f"{marker} {i+1:4d}: {lines[i]}")

    return ("", "\n".join(snippet_lines), False)


def fetch_file_snippet(
    file_path: str,
    line_number: int,
    repo_paths: dict[str, str],
    context_lines: int = 15,
) -> FileSnippet | None:
    """
    Fetch a code snippet from a local file.

    Args:
        file_path: Original path from stack trace
        line_number: Line number from stack trace
        repo_paths: Dict mapping repo names to local paths
        context_lines: Number of lines before/after to include

    Returns:
        FileSnippet if file found, None otherwise
    """
    repo_name = infer_repo_from_path(file_path)
    normalized_path = normalize_file_path(file_path)

    # Try to find the file
    candidates = []

    # 1. Try the file path directly (might be absolute and exist)
    if Path(file_path).exists():
        candidates.append((Path(file_path), repo_name or "unknown"))

    # 2. Try in known repo paths
    if repo_name and repo_name in repo_paths:
        repo_root = Path(repo_paths[repo_name])
        candidates.append((repo_root / normalized_path, repo_name))
        # Also try without the repo prefix in the path
        for prefix in DEFAULT_PATH_PREFIXES:
            if normalized_path.startswith(prefix):
                stripped = normalized_path[len(prefix) :]
                candidates.append((repo_root / stripped, repo_name))

    # 3. Try in all repo paths
    for rname, rpath in repo_paths.items():
        repo_root = Path(rpath)
        candidates.append((repo_root / normalized_path, rname))

    # Find the first existing file
    for candidate_path, rname in candidates:
        if candidate_path.exists() and candidate_path.is_file():
            # Try to extract full function
            result = extract_full_function(candidate_path, line_number)
            if result:
                func_name, content, is_full = result
                return FileSnippet(
                    file_path=file_path,
                    repo_name=rname,
                    line_number=line_number,
                    content=content,
                    local_path=str(candidate_path),
                    function_name=func_name,
                    is_full_function=is_full,
                )

    return None


def gather_code_context(
    log_content: str,
    repo_paths: dict[str, str] | None = None,
    max_snippets: int = 10,
) -> CodeContext:
    """
    Extract code context from stack traces in the log.

    Extracts full functions (not just snippets) from the call stack,
    providing the LLM with complete context about what the code was doing.

    Args:
        log_content: The log content (or error sections)
        repo_paths: Dict mapping repo names to local paths (auto-discovered if None)
        max_snippets: Maximum number of code snippets to include

    Returns:
        CodeContext with code snippets from the stack trace
    """
    if repo_paths is None:
        # Auto-discover from the log content itself
        repo_paths = discover_repo_paths(log_content=log_content)

    ctx = CodeContext(repo_paths=repo_paths)

    # Extract file references from the log (ordered by stack depth: deepest first)
    file_refs = extract_files_from_stack(log_content)

    # Fetch full functions for each stack frame
    seen_files = set()  # Avoid duplicate files
    for depth, (file_path, line_num) in enumerate(file_refs[: max_snippets * 2]):
        # Skip duplicates (same file may appear multiple times in stack)
        file_key = f"{file_path}:{line_num}"
        if file_key in seen_files:
            continue
        seen_files.add(file_key)

        snippet = fetch_file_snippet(file_path, line_num, repo_paths)
        if snippet:
            snippet.stack_depth = depth
            ctx.snippets.append(snippet)
            if len(ctx.snippets) >= max_snippets:
                break

    return ctx


def format_context_for_prompt(ctx: CIContext) -> str:
    """Format context for LLM prompt."""
    parts = []

    if ctx.job.job_name:
        parts.append("JOB INFORMATION:")
        parts.append(f"  Name: {ctx.job.job_name}")
        parts.append(f"  Command: {ctx.job.cmd}")
        parts.append(f"  Team: {ctx.job.team}")
        parts.append(f"  Owner ID: {ctx.job.owner_id}")
        parts.append(f"  Timeout: {ctx.job.timeout_minutes} minutes")

    if ctx.pr.branch:
        parts.append("\nBRANCH/PR INFORMATION:")
        parts.append(f"  Branch: {ctx.pr.branch}")
        if ctx.pr.pr_number:
            parts.append(f"  PR: #{ctx.pr.pr_number}")

    if ctx.pr.changed_files:
        parts.append("\nCHANGED FILES IN THIS PR/BRANCH:")
        for f in ctx.pr.changed_files[:30]:  # Cap at 30 files
            parts.append(f"  - {f}")
        if len(ctx.pr.changed_files) > 30:
            parts.append(f"  ... and {len(ctx.pr.changed_files) - 30} more")

    # Include code snippets from stack traces
    if ctx.code.snippets:
        parts.append("\n" + "=" * 60)
        parts.append("CODE CONTEXT FROM CALL STACK")
        parts.append("=" * 60)
        parts.append("Full functions from the call stack (deepest/error location first).")
        parts.append("Line with >>> is the exact location in the stack trace.\n")

        for snippet in ctx.code.snippets:
            # Header with repo, file, and function name
            header = f"### [{snippet.repo_name}] {snippet.file_path}"
            if snippet.function_name:
                header += f" :: {snippet.function_name}()"
            parts.append(header)

            # Metadata line
            meta = f"(Line {snippet.line_number}"
            if snippet.is_full_function:
                meta += ", full function"
            if snippet.stack_depth > 0:
                meta += f", stack depth {snippet.stack_depth}"
            meta += ")"
            parts.append(meta)

            # Determine language for syntax highlighting
            suffix = Path(snippet.file_path).suffix.lower()
            lang = {".py": "python", ".cpp": "cpp", ".cc": "cpp", ".c": "c", ".h": "cpp"}.get(suffix, "")
            parts.append(f"```{lang}")
            parts.append(snippet.content)
            parts.append("```\n")

    return "\n".join(parts)
