# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
CLI integration tests — real-world scenarios exercising the full pipeline.

Each test builds a JSON config pointing at a realistic log directory
structure and runs main(). The tool writes both .md and .json files
directly to output_dir. Tests read those files directly.

Fixture log samples in fixtures/log_samples/ are real CI logs.
Mock LLM responses in fixtures/mock_responses/ match those logs.
"""

import json
import re
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_job_summary.cli import main, _resolve_log_dirs, _job_id_from_url

from .conftest import FIXTURE_LOG_DIR, FIXTURE_RESP_DIR


# ── Helpers ───────────────────────────────────────────────────────────────────


def _config_json(tmp_path: Path, input_dirs: list[str], **extra) -> str:
    """Build a JSON config string pointing at input_dirs under tmp_path.

    input_dirs are resolved to absolute paths under tmp_path so the tool
    finds them regardless of cwd; output_dir is also tmp_path.
    """
    resolved_dirs = [str(tmp_path / d) if not Path(d).is_absolute() else d for d in input_dirs]
    config = {
        "model": "test-model",
        "workspace": str(tmp_path),
        "input_dirs": resolved_dirs,
        "output_dir": str(tmp_path),
        **extra,
    }
    return json.dumps(config)


def _write_log(dir_path: Path, name: str, content: str) -> Path:
    """Write a .log file into a directory."""
    dir_path.mkdir(parents=True, exist_ok=True)
    f = dir_path / name
    f.write_text(content)
    return f


def _run_cli(args: list[str]) -> None:
    """Run main() with given args."""
    with patch("sys.argv", ["ai-job-summary"] + args):
        main()


def _read_summary(output_dir: Path, suffix: str = ".md") -> str:
    """Read the first summary file with the given suffix from output_dir."""
    summaries = list(output_dir.glob(f"*{suffix}"))
    assert summaries, f"No {suffix} summary found in {output_dir}"
    return summaries[0].read_text()


def _mock_llm_response_from_fixture(fixture_name: str) -> MagicMock:
    """Load a mock LLM response from fixtures/mock_responses/."""
    resp = MagicMock()
    resp.content = (FIXTURE_RESP_DIR / f"{fixture_name}.json").read_text()
    resp.model = "test-model"
    resp.prompt_tokens = 100
    resp.completion_tokens = 50
    resp.response_time_ms = 500.0
    return resp


def _mock_llm_response(status="CRASH", category="app:cli", root_cause="server failed", error_message="error", **extra):
    """Create an inline mock LLM response."""
    resp = MagicMock()
    resp.content = json.dumps(
        {
            "status": status,
            "category": category,
            "root_cause": root_cause,
            "error_message": error_message,
            "failed_tests": [],
            "suggested_action": "fix it",
            "confidence": "high",
            **extra,
        }
    )
    resp.model = "test-model"
    resp.prompt_tokens = 100
    resp.completion_tokens = 50
    resp.response_time_ms = 500.0
    return resp


# ── _job_id_from_url ──────────────────────────────────────────────────────────


class TestJobIdFromUrl:
    def test_extracts_id(self):
        url = "https://github.com/org/repo/actions/runs/123/job/456"
        assert _job_id_from_url(url) == "456"

    def test_no_job_segment(self):
        url = "https://github.com/org/repo/actions/runs/123"
        assert _job_id_from_url(url) == ""

    def test_empty(self):
        assert _job_id_from_url("") == ""


# ── resolve_log_dirs ──────────────────────────────────────────────────────────


class TestResolveLogDirs:
    def test_all_present(self, tmp_path):
        _write_log(tmp_path / "run_logs", "run.log", "some log content\n")
        _write_log(tmp_path / "docker_server", "server.log", "server log\n")
        present, missing = _resolve_log_dirs(["run_logs", "docker_server"], tmp_path)
        assert len(present) == 2
        assert missing == []

    def test_one_missing(self, tmp_path):
        _write_log(tmp_path / "run_logs", "run.log", "some log content\n")
        present, missing = _resolve_log_dirs(["run_logs", "docker_server"], tmp_path)
        assert len(present) == 1
        assert missing == ["docker_server"]

    def test_dir_exists_but_no_logs(self, tmp_path):
        (tmp_path / "empty_dir").mkdir()
        present, missing = _resolve_log_dirs(["empty_dir"], tmp_path)
        assert present == []
        assert missing == ["empty_dir"]

    def test_all_missing(self, tmp_path):
        present, missing = _resolve_log_dirs(["run_logs", "docker_server"], tmp_path)
        assert present == []
        assert len(missing) == 2

    def test_absolute_log_dirs_ignore_base(self, tmp_path):
        # Pathlib: Path(base) / Path(absolute) returns the absolute.
        _write_log(tmp_path / "logs", "run.log", "ok\n")
        present, missing = _resolve_log_dirs([str(tmp_path / "logs")], Path("/nonexistent"))
        assert len(present) == 1
        assert missing == []


# ── --config validation ───────────────────────────────────────────────────────


class TestConfigParsing:
    def test_invalid_json_exits(self, capsys):
        with pytest.raises(SystemExit):
            _run_cli(["--config", "{not valid json"])
        assert "Invalid JSON" in capsys.readouterr().err

    def test_non_object_json_exits(self, capsys):
        with pytest.raises(SystemExit):
            _run_cli(["--config", "[1, 2, 3]"])
        assert "must be a JSON object" in capsys.readouterr().err

    def test_missing_log_dirs_exits(self, tmp_path):
        config = _config_json(tmp_path, [])
        with pytest.raises(SystemExit):
            _run_cli(["--config", config])

    def test_missing_output_dir_exits(self, tmp_path):
        config = json.dumps(
            {
                "model": "test",
                "workspace": str(tmp_path),
                "input_dirs": ["run_logs"],
            }
        )
        with pytest.raises(SystemExit):
            _run_cli(["--config", config])

    def test_dotdot_in_input_dirs_exits(self, tmp_path, capsys):
        config = json.dumps(
            {
                "model": "test",
                "workspace": str(tmp_path),
                "input_dirs": ["../escape"],
                "output_dir": str(tmp_path),
            }
        )
        with pytest.raises(SystemExit):
            _run_cli(["--config", config])
        assert "must not contain '..'" in capsys.readouterr().err

    def test_dotdot_in_output_dir_exits(self, tmp_path, capsys):
        config = json.dumps(
            {
                "model": "test",
                "workspace": str(tmp_path),
                "input_dirs": ["logs"],
                "output_dir": "../out",
            }
        )
        with pytest.raises(SystemExit):
            _run_cli(["--config", config])
        assert "must not contain '..'" in capsys.readouterr().err


# ── workspace anchoring ───────────────────────────────────────────────────────


class TestWorkspace:
    """workspace field anchors relative input_dirs and output_dir."""

    def test_relative_input_dirs_resolve_against_workspace(self, tmp_path):
        _write_log(tmp_path / "logs", "run.log", "INFO: ok\n")
        config = json.dumps(
            {
                "model": "test",
                "workspace": str(tmp_path),
                "input_dirs": ["logs"],
                "output_dir": str(tmp_path),
            }
        )
        _run_cli(["--config", config])
        assert any(tmp_path.glob("ai_job_summary*.md"))

    def test_relative_output_dir_resolves_against_workspace(self, tmp_path):
        _write_log(tmp_path / "logs", "run.log", "INFO: ok\n")
        config = json.dumps(
            {
                "model": "test",
                "workspace": str(tmp_path),
                "input_dirs": ["logs"],
                "output_dir": "out",
            }
        )
        _run_cli(["--config", config])
        assert any((tmp_path / "out").glob("ai_job_summary*.md"))

    def test_env_vars_in_paths_are_expanded(self, tmp_path, monkeypatch):
        # Caller writes "$GITHUB_WORKSPACE/foo"; tool expands against env.
        # This is the only way to express the in-container workspace path
        # for container jobs (the ${{ github.workspace }} expression
        # always resolves to the host path).
        _write_log(tmp_path / "logs", "run.log", "INFO: ok\n")
        monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
        config = json.dumps(
            {
                "model": "test",
                "workspace": "$GITHUB_WORKSPACE",
                "input_dirs": ["logs"],
                "output_dir": "out",
            }
        )
        _run_cli(["--config", config])
        assert any((tmp_path / "out").glob("ai_job_summary*.md"))


# ── Dual output ───────────────────────────────────────────────────────────────


class TestDualOutput:
    """Tool always writes both .md and .json."""

    def test_writes_both_files(self, tmp_path):
        _write_log(tmp_path / "run_logs", "run.log", "INFO: all good\n")
        _write_log(tmp_path / "docker_server", "server.log", "INFO: server ready\n")
        config = _config_json(tmp_path, ["run_logs", "docker_server"])
        _run_cli(["--config", config])
        assert any(tmp_path.glob("*.md"))
        assert any(tmp_path.glob("*.json"))

    def test_json_has_job_block(self, tmp_path):
        _write_log(tmp_path / "run_logs", "run.log", "INFO: all good\n")
        _write_log(tmp_path / "docker_server", "server.log", "INFO: server ready\n")
        config = _config_json(tmp_path, ["run_logs", "docker_server"])
        _run_cli(["--config", config])
        data = json.loads(_read_summary(tmp_path, ".json"))
        assert "_job" in data
        assert "status" in data["_job"]

    def test_filename_uses_job_id_from_url(self, tmp_path):
        _write_log(tmp_path / "run_logs", "run.log", "INFO: all good\n")
        _write_log(tmp_path / "docker_server", "server.log", "INFO: server ready\n")
        config = _config_json(tmp_path, ["run_logs", "docker_server"])
        _run_cli(["--config", config, "--job-url", "https://github.com/org/repo/actions/runs/1/job/99999"])
        assert (tmp_path / "ai_job_summary_99999.md").exists()
        assert (tmp_path / "ai_job_summary_99999.json").exists()

    def test_fallback_filename_without_job_url(self, tmp_path):
        """When no --job-url, files named ai_job_summary.md/.json (no ID suffix)."""
        _write_log(tmp_path / "run_logs", "run.log", "INFO: all good\n")
        _write_log(tmp_path / "docker_server", "server.log", "INFO: server ready\n")
        config = _config_json(tmp_path, ["run_logs", "docker_server"])
        _run_cli(["--config", config])
        assert any(tmp_path.glob("ai_job_summary*.md"))
        assert any(tmp_path.glob("ai_job_summary*.json"))


# ── Infra failure: no logs at all ─────────────────────────────────────────────


class TestInfraFailureNoLogs:
    """All log_dirs are missing → INFRA_FAILURE without LLM call."""

    def test_outputs_infra_failure(self, tmp_path):
        config = _config_json(tmp_path, ["nonexistent/run_logs", "nonexistent/docker_server"])
        _run_cli(["--config", config])
        md = _read_summary(tmp_path, ".md")
        assert "INFRA FAILURE" in md

    def test_json_has_infra_failure_status(self, tmp_path):
        config = _config_json(tmp_path, ["nonexistent/a", "nonexistent/b"])
        _run_cli(["--config", config])
        data = json.loads(_read_summary(tmp_path, ".json"))
        assert data["_job"]["status"] == "INFRA FAILURE"
        assert data["category"] == "infra:no_logs"

    def test_json_includes_job_name_and_url(self, tmp_path):
        config = _config_json(tmp_path, ["nonexistent/a"])
        _run_cli(["--config", config, "--job-name", "my-job", "--job-url", "https://example.com/job/123"])
        data = json.loads(_read_summary(tmp_path, ".json"))
        assert data["_job"]["name"] == "my-job"
        assert data["_job"]["url"] == "https://example.com/job/123"

    def test_header_matches_action_grep_pattern(self, tmp_path):
        """Markdown header must match ^### .* INFRA FAILURE for consistency."""
        config = _config_json(tmp_path, ["nonexistent/a"])
        _run_cli(["--config", config])
        md = _read_summary(tmp_path, ".md")
        assert re.search(r"^### .* INFRA FAILURE", md, re.MULTILINE)

    def test_writes_both_files(self, tmp_path):
        """Infra-failure path also writes both .md and .json."""
        config = _config_json(tmp_path, ["nonexistent/a"])
        _run_cli(["--config", config])
        assert any(tmp_path.glob("*.md"))
        assert any(tmp_path.glob("*.json"))


# ── Pre-LLM timeout ──────────────────────────────────────────────────────────


class TestPreLLMTimeout:
    """TimeoutError during extraction or context gathering → INFRA_FAILURE."""

    def test_extraction_timeout_writes_infra_failure(self, tmp_path):
        run_logs = tmp_path / "run_logs"
        run_logs.mkdir()
        (run_logs / "test.log").write_text("TT_FATAL error: crash\n")
        config = _config_json(tmp_path, ["run_logs"])

        with patch("ai_job_summary.cli.extract_log", side_effect=TimeoutError("timed out")):
            _run_cli(["--config", config])

        data = json.loads(_read_summary(tmp_path, ".json"))
        assert data["_job"]["status"] == "INFRA FAILURE"
        assert data["category"] == "infra:timeout"
        assert "timed out" in data["root_cause"]

    def test_context_timeout_writes_infra_failure(self, tmp_path):
        run_logs = tmp_path / "run_logs"
        run_logs.mkdir()
        (run_logs / "test.log").write_text("TT_FATAL error: crash\n")
        config = _config_json(tmp_path, ["run_logs"])

        with patch("ai_job_summary.cli.gather_config_context", side_effect=TimeoutError("timed out")):
            _run_cli(["--config", config])

        data = json.loads(_read_summary(tmp_path, ".json"))
        assert data["_job"]["status"] == "INFRA FAILURE"
        assert data["category"] == "infra:timeout"

    def test_timeout_writes_both_files(self, tmp_path):
        run_logs = tmp_path / "run_logs"
        run_logs.mkdir()
        (run_logs / "test.log").write_text("TT_FATAL error: crash\n")
        config = _config_json(tmp_path, ["run_logs"])

        with patch("ai_job_summary.cli.extract_log", side_effect=TimeoutError("timed out")):
            _run_cli(["--config", config])

        assert any(tmp_path.glob("*.md"))
        assert any(tmp_path.glob("*.json"))


# ── Infra failure: real CI scenario — Docker pull failure ─────────────────────


class TestInfraFailureDockerPull:
    """
    Real CI job: Docker image pull failed → docker_server/ never created.
    Fixture from: github.com/tenstorrent/tt-shield/actions/runs/23458114088/job/68252606441
    """

    def test_missing_docker_server_is_infra_failure(self, tmp_path):
        """run_logs present (with Docker error), docker_server absent → INFRA_FAILURE."""
        fixture = FIXTURE_LOG_DIR / "infra_docker_pull_failure.txt"
        run_logs = tmp_path / "run_logs"
        run_logs.mkdir()
        (run_logs / "run.log").write_text(fixture.read_text())

        mock_llm = MagicMock()
        mock_llm.chat.return_value = _mock_llm_response(
            status="CRASH",
            category="infra:docker",
            root_cause="Docker pull failed",
        )

        config = _config_json(tmp_path, ["run_logs", "docker_server"])
        with patch("ai_job_summary.cli.get_llm_client", return_value=mock_llm):
            _run_cli(["--config", config])
        md = _read_summary(tmp_path, ".md")
        assert "INFRA FAILURE" in md


# ── Infra failure: partial logs ───────────────────────────────────────────────


class TestInfraFailurePartialLogs:
    """run_logs present but docker_server missing → INFRA_FAILURE."""

    def test_any_missing_dir_is_infra_failure(self, tmp_path):
        _write_log(tmp_path / "run_logs", "run.log", "INFO: some log content\n")
        config = _config_json(tmp_path, ["run_logs", "docker_server"])
        _run_cli(["--config", config])
        md = _read_summary(tmp_path, ".md")
        assert "INFRA FAILURE" in md

    def test_partial_logs_with_no_errors(self, tmp_path):
        """Partial logs with no errors → INFRA_FAILURE without LLM."""
        _write_log(tmp_path / "run_logs", "run.log", "INFO: some log content\n")
        config = _config_json(tmp_path, ["run_logs", "docker_server"])

        with patch("ai_job_summary.cli.get_llm_client") as mock_llm:
            _run_cli(["--config", config])
            mock_llm.assert_not_called()

        data = json.loads(_read_summary(tmp_path, ".json"))
        assert data["_job"]["status"] == "INFRA FAILURE"
        assert data["category"] == "infra:partial_logs"

    def test_partial_logs_with_errors_calls_llm(self, tmp_path):
        """Partial logs with errors → LLM called for analysis, status stays INFRA FAILURE."""
        fixture = FIXTURE_LOG_DIR / "infra_docker_pull_failure.txt"
        _write_log(tmp_path / "run_logs", "run.log", fixture.read_text())
        config = _config_json(tmp_path, ["run_logs", "docker_server"])

        mock_llm = MagicMock()
        mock_llm.chat.return_value = _mock_llm_response(
            status="CRASH",
            category="infra:docker",
            root_cause="Docker pull failed",
        )

        with patch("ai_job_summary.cli.get_llm_client", return_value=mock_llm):
            _run_cli(["--config", config])

        mock_llm.chat.assert_called_once()
        data = json.loads(_read_summary(tmp_path, ".json"))
        assert data["_job"]["status"] == "INFRA FAILURE"
        assert data.get("root_cause")  # LLM populated this

    def test_all_logs_present_but_llm_promotes_to_infra(self, tmp_path):
        """All dirs present + LLM classifies category as infra:* → status promoted to INFRA FAILURE.

        Tests the cli.py promotion path that's distinct from missing-dirs:
        no log_dir is missing, extraction does see a failure (so we reach
        the LLM), and the LLM's category starts with `infra:` → status is
        forced to INFRA_FAILURE regardless of LLM's status field.
        """
        from ai_job_summary.extract import JobStatus

        _write_log(tmp_path / "run_logs", "run.log", "ERROR something failed\n")
        _write_log(tmp_path / "docker_server", "server.log", "ERROR something failed\n")
        config = _config_json(tmp_path, ["run_logs", "docker_server"])

        mock_llm = MagicMock()
        mock_llm.chat.return_value = _mock_llm_response(
            status="CRASH",
            category="infra:network",
            root_cause="DNS resolution failure",
        )
        # Force extraction status to non-SUCCESS so the CLI reaches the LLM
        # branch (extraction-pattern-driven status from synthetic logs is
        # too fragile for this test; we just want the promotion path).
        forced_status = JobStatus(False, "RED", "FAILED")

        with patch("ai_job_summary.cli.get_llm_client", return_value=mock_llm), patch(
            "ai_job_summary.cli.get_job_status", return_value=forced_status
        ):
            _run_cli(["--config", config])

        data = json.loads(_read_summary(tmp_path, ".json"))
        # Status promoted to INFRA FAILURE despite no missing dirs.
        assert data["_job"]["status"] == "INFRA FAILURE"
        assert data["category"] == "infra:network"


# ── Success: all logs present, no errors ──────────────────────────────────────


class TestSuccess:
    """All log dirs present, logs show no errors → SUCCESS, no LLM call."""

    @pytest.fixture
    def success_dir(self, tmp_path):
        _write_log(
            tmp_path / "run_logs",
            "run.log",
            textwrap.dedent(
                """\
            2026-03-15 09:00:00,000 - run.py:100 - INFO: Starting server
            2026-03-15 09:00:01,000 - server.py:50 - INFO: Server started on port 8000
            2026-03-15 09:01:30,500 - run_workflows.py:101 - INFO: workflow: benchmarks completed with return code: 0
            2026-03-15 09:02:46,000 - run.py:550 - INFO: All workflows completed successfully
        """
            ),
        )
        _write_log(tmp_path / "docker_server", "server.log", "INFO: vLLM server ready\nINFO: Serving requests\n")
        return tmp_path

    def test_outputs_success(self, success_dir):
        config = _config_json(success_dir, ["run_logs", "docker_server"])
        _run_cli(["--config", config])
        md = _read_summary(success_dir, ".md")
        assert "SUCCESS" in md

    def test_no_llm_call(self, success_dir):
        config = _config_json(success_dir, ["run_logs", "docker_server"])
        with patch("ai_job_summary.cli.get_llm_client") as mock_llm:
            _run_cli(["--config", config])
            mock_llm.assert_not_called()

    def test_job_name_in_json(self, success_dir):
        config = _config_json(success_dir, ["run_logs", "docker_server"])
        _run_cli(["--config", config, "--job-name", "Llama-3.1-8B (n150)"])
        data = json.loads(_read_summary(success_dir, ".json"))
        assert data["_job"]["name"] == "Llama-3.1-8B (n150)"


# ── Crash: LLM overrides extraction (using real fixture + mock response) ──────


class TestLLMCrashOverride:
    """
    Server failed to start (argparse error). Extraction sees
    'workflow:benchmarks' as a failed test, but LLM says CRASH.
    Uses fixture log and mock response files.
    """

    def test_llm_crash_overrides_tests_failed(self, tmp_path):
        _write_log(
            tmp_path / "run_logs",
            "run.log",
            textwrap.dedent(
                """\
            error: unrecognized arguments: --num-scheduler-steps
            2026-03-23 01:18:53 - run_workflows.py:108 - ERROR: workflow: evals, failed with return code: 1
            2026-03-23 01:39:05 - run_workflows.py:108 - ERROR: workflow: benchmarks, failed with return code: 1
            Process completed with exit code 1
        """
            ),
        )
        _write_log(tmp_path / "docker_server", "server.log", "INFO: server starting\n")
        config = _config_json(tmp_path, ["run_logs", "docker_server"])

        mock_llm = MagicMock()
        mock_llm.chat.return_value = _mock_llm_response_from_fixture("app_cli_argparse")

        with patch("sys.argv", ["ai-job-summary", "--config", config]), patch(
            "ai_job_summary.cli.get_llm_client", return_value=mock_llm
        ):
            main()

        md = _read_summary(tmp_path, ".md")
        assert "CRASHED" in md
        assert "TESTS FAILED" not in md
