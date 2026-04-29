# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for ai_run_summary.cli."""

import json
import os
from unittest.mock import patch

import pytest

from ai_run_summary.cli import main, _should_call_llm, _resolve_run_metadata, synthesize_missing_legs


class TestShouldCallLlm:
    def test_none_string(self):
        assert _should_call_llm({"model": "none"}) is False

    def test_none_uppercase(self):
        assert _should_call_llm({"model": "NONE"}) is False

    def test_empty_string(self):
        assert _should_call_llm({"model": ""}) is False

    def test_missing_key(self):
        assert _should_call_llm({}) is False

    def test_valid_model(self):
        assert _should_call_llm({"model": "anthropic/claude-sonnet-4-5-20250929"}) is True


class TestResolveRunMetadata:
    def test_empty_env(self):
        with patch.dict(os.environ, {}, clear=True):
            meta = _resolve_run_metadata()
            assert meta["run_id"] == ""
            assert meta["run_url"] == ""
            assert meta["pr"] == ""
            assert meta["run_date"]  # always set to today

    def test_github_env(self):
        env = {
            "GITHUB_RUN_ID": "99999",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "org/repo",
        }
        with patch.dict(os.environ, env, clear=True):
            meta = _resolve_run_metadata()
            assert meta["run_id"] == "99999"
            assert meta["run_url"] == "https://github.com/org/repo/actions/runs/99999"

    def test_pr_detection(self):
        env = {
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REF": "refs/pull/42/merge",
        }
        with patch.dict(os.environ, env, clear=True):
            meta = _resolve_run_metadata()
            assert meta["pr"] == "42"

    def test_no_pr_for_push(self):
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}
        with patch.dict(os.environ, env, clear=True):
            meta = _resolve_run_metadata()
            assert meta["pr"] == ""


class TestMain:
    def _config_json(self, tmp_path, input_dir=None, output_dir=None, model="none"):
        """Build a JSON config string for the run-summary CLI."""
        config = {"workspace": str(tmp_path)}
        if model:
            config["model"] = model
        if input_dir is not None:
            config["input_dir"] = str(input_dir)
        if output_dir is not None:
            config["output_dir"] = str(output_dir)
        return json.dumps(config)

    def _write_summaries(self, summaries_dir):
        """Write a minimal summary JSON to the output_dir."""
        ai_dir = summaries_dir
        ai_dir.mkdir(parents=True, exist_ok=True)
        (ai_dir / "test.json").write_text(
            json.dumps(
                {
                    "_job": {
                        "name": "test-job",
                        "status": "SUCCESS",
                        "status_code": "GREEN",
                    }
                }
            )
        )
        return ai_dir

    def test_missing_config_exits(self):
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", ["ai-run-summary"]):
                main()
        assert exc.value.code == 2

    def test_invalid_json_config_exits(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", ["ai-run-summary", "--config", "{not json"]):
                main()
        assert exc.value.code == 1
        assert "Invalid JSON" in capsys.readouterr().err

    def test_non_object_config_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", ["ai-run-summary", "--config", "[1,2]"]):
                main()
        assert exc.value.code == 1
        assert "must be a JSON object" in capsys.readouterr().err

    def test_missing_output_dir_exits(self, tmp_path):
        config_str = self._config_json(tmp_path, input_dir=str(tmp_path))  # no output_dir
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
                main()
        assert exc.value.code == 1

    def test_missing_input_dir_exits(self, tmp_path):
        config_str = self._config_json(tmp_path, output_dir=str(tmp_path))  # no input_dir
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
                main()
        assert exc.value.code == 1

    def test_dotdot_in_input_dir_exits(self, tmp_path, capsys):
        config_str = self._config_json(tmp_path, input_dir="../escape", output_dir="out")
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
                main()
        assert "must not contain '..'" in capsys.readouterr().err

    def test_dotdot_in_output_dir_exits(self, tmp_path, capsys):
        config_str = self._config_json(tmp_path, input_dir="in", output_dir="../out")
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
                main()
        assert "must not contain '..'" in capsys.readouterr().err

    def test_model_none_skips_llm(self, tmp_path):
        self._write_summaries(tmp_path)
        config_str = self._config_json(tmp_path, input_dir=str(tmp_path), output_dir=str(tmp_path), model="none")
        with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "12345"}, clear=False):
                main()
        report = tmp_path / "ai_run_summary_12345.md"
        assert report.exists()
        assert "AI Run Summary" in report.read_text()

    def test_empty_summaries_dir_warns(self, tmp_path, capsys):
        tmp_path.mkdir(parents=True, exist_ok=True)
        config_str = self._config_json(tmp_path, input_dir=str(tmp_path), output_dir=str(tmp_path), model="none")
        with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
            main()
        stderr = capsys.readouterr().err
        assert "No summary files found" in stderr

    def test_run_metadata_from_env(self, tmp_path):
        self._write_summaries(tmp_path)
        config_str = self._config_json(tmp_path, input_dir=str(tmp_path), output_dir=str(tmp_path), model="none")
        env = {
            "GITHUB_RUN_ID": "77777",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "org/repo",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
                main()
        report = tmp_path / "ai_run_summary_77777.md"
        assert report.exists()
        assert "77777" in report.read_text()

    def test_relative_dirs_resolve_against_workspace(self, tmp_path):
        # Workspace is tmp_path; input_dir is "summaries" (relative).
        sub = tmp_path / "summaries"
        self._write_summaries(sub)
        config_str = json.dumps(
            {
                "workspace": str(tmp_path),
                "model": "none",
                "input_dir": "summaries",
                "output_dir": "out",
            }
        )
        with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "99"}, clear=False):
                main()
        assert (tmp_path / "out" / "ai_run_summary_99.md").exists()
        # .html is written alongside .md and consumed by Slack screenshot.
        assert (tmp_path / "out" / "ai_run_summary_99.html").exists()

    def test_env_vars_in_workspace_expanded(self, tmp_path, monkeypatch):
        """Caller writes "$GITHUB_WORKSPACE"; tool expands at runtime.

        Mirrors the job-side test — expansion is needed inside container
        jobs where the github.workspace expression renders the host path.
        """
        sub = tmp_path / "summaries"
        self._write_summaries(sub)
        monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
        config_str = json.dumps(
            {
                "workspace": "$GITHUB_WORKSPACE",
                "model": "none",
                "input_dir": "summaries",
                "output_dir": "out",
            }
        )
        with patch("sys.argv", ["ai-run-summary", "--config", config_str]):
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "55"}, clear=False):
                main()
        assert (tmp_path / "out" / "ai_run_summary_55.md").exists()

    def test_expected_jobs_without_run_result_hard_fails(self, tmp_path, capsys):
        self._write_summaries(tmp_path)
        config_str = self._config_json(tmp_path, input_dir=str(tmp_path), output_dir=str(tmp_path), model="none")
        argv = ["ai-run-summary", "--config", config_str, "--expected-jobs", '[{"name":"X"}]']
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", argv):
                main()
        assert exc.value.code == 1
        stderr = capsys.readouterr().err
        assert "::error::" in stderr
        assert "must be passed together" in stderr

    def test_run_result_without_expected_jobs_hard_fails(self, tmp_path, capsys):
        self._write_summaries(tmp_path)
        config_str = self._config_json(tmp_path, input_dir=str(tmp_path), output_dir=str(tmp_path), model="none")
        argv = ["ai-run-summary", "--config", config_str, "--run-result", "failure"]
        with pytest.raises(SystemExit) as exc:
            with patch("sys.argv", argv):
                main()
        assert exc.value.code == 1
        stderr = capsys.readouterr().err
        assert "::error::" in stderr
        assert "must be passed together" in stderr


class TestSynthesizeMissingLegs:
    """Expected-jobs reconciliation: stubs INFRA_FAILURE for missing artifacts."""

    @staticmethod
    def _write_summary(summary_dir, name, status="SUCCESS"):
        summary_dir.mkdir(parents=True, exist_ok=True)
        path = summary_dir / f"ai_job_summary_{abs(hash(name))}.json"
        path.write_text(json.dumps({"_job": {"name": name, "status": status}}))

    def test_stubs_infra_for_missing_when_run_failed(self, tmp_path):
        summary_dir = tmp_path / "summaries"
        self._write_summary(summary_dir, "[N150] Alpha")
        expected = [{"name": "[N150] Alpha"}, {"name": "[N150] Beta"}]

        stats = synthesize_missing_legs(summary_dir, expected, run_result="failure")

        assert stats == {"infra_stubbed": 1}
        produced = {
            json.loads(f.read_text())["_job"]["name"]: json.loads(f.read_text())["_job"]["status"]
            for f in summary_dir.glob("*.json")
        }
        assert produced == {"[N150] Alpha": "SUCCESS", "[N150] Beta": "INFRA FAILURE"}

    def test_stubs_infra_for_missing_when_run_succeeded(self, tmp_path):
        summary_dir = tmp_path / "summaries"
        self._write_summary(summary_dir, "[N150] Alpha")
        expected = [{"name": "[N150] Alpha"}, {"name": "[N150] Beta"}]

        stats = synthesize_missing_legs(summary_dir, expected, run_result="success")

        assert stats == {"infra_stubbed": 1}

    def test_cancelled_run_does_not_synthesize(self, tmp_path):
        summary_dir = tmp_path / "summaries"
        self._write_summary(summary_dir, "[N150] Alpha")
        expected = [{"name": "[N150] Alpha"}, {"name": "[N150] Beta"}]

        stats = synthesize_missing_legs(summary_dir, expected, run_result="cancelled")

        assert stats == {"infra_stubbed": 0}
        remaining = {json.loads(f.read_text())["_job"]["name"] for f in summary_dir.glob("*.json")}
        assert remaining == {"[N150] Alpha"}

    def test_accepts_json_string(self, tmp_path):
        summary_dir = tmp_path / "summaries"
        expected_str = json.dumps([{"name": "[N150] Alpha"}])

        stats = synthesize_missing_legs(summary_dir, expected_str, run_result="failure")

        assert stats == {"infra_stubbed": 1}
        data = json.loads(next(summary_dir.glob("*.json")).read_text())
        assert data["_job"]["name"] == "[N150] Alpha"
        assert data["_job"]["status"] == "INFRA FAILURE"

    def test_all_arrived_no_synthesis(self, tmp_path):
        summary_dir = tmp_path / "summaries"
        self._write_summary(summary_dir, "[N150] Alpha")
        self._write_summary(summary_dir, "[N150] Beta")
        expected = [{"name": "[N150] Alpha"}, {"name": "[N150] Beta"}]

        stats = synthesize_missing_legs(summary_dir, expected, run_result="success")

        assert stats == {"infra_stubbed": 0}

    def test_empty_expected_list(self, tmp_path):
        summary_dir = tmp_path / "summaries"
        stats = synthesize_missing_legs(summary_dir, [], run_result="failure")
        assert stats == {"infra_stubbed": 0}

    def test_skipped_run_does_not_synthesize(self, tmp_path):
        # run_result=skipped means the matrix was gated off — nothing ran,
        # nothing to reconcile against.
        summary_dir = tmp_path / "summaries"
        expected = [{"name": "[N150] Alpha"}, {"name": "[N150] Beta"}]

        stats = synthesize_missing_legs(summary_dir, expected, run_result="skipped")

        assert stats == {"infra_stubbed": 0}

    def test_malformed_json_string_warns_and_returns_zero(self, tmp_path, capsys):
        summary_dir = tmp_path / "summaries"

        stats = synthesize_missing_legs(summary_dir, "{not valid json", run_result="failure")

        assert stats == {"infra_stubbed": 0}
        captured = capsys.readouterr()
        assert "::warning::" in captured.err
        assert "not valid JSON" in captured.err

    def test_duplicate_names_in_expected_counted_once(self, tmp_path):
        summary_dir = tmp_path / "summaries"
        expected = [{"name": "[N150] Alpha"}, {"name": "[N150] Alpha"}]

        stats = synthesize_missing_legs(summary_dir, expected, run_result="failure")

        assert stats == {"infra_stubbed": 1}
        files = list(summary_dir.glob("*.json"))
        assert len(files) == 1

    def test_stub_filename_is_deterministic(self, tmp_path):
        # Two runs against different directories should produce the same
        # filename slug for the same job name (no PYTHONHASHSEED randomness).
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        synthesize_missing_legs(dir_a, [{"name": "[N150] Alpha"}], run_result="failure")
        synthesize_missing_legs(dir_b, [{"name": "[N150] Alpha"}], run_result="failure")
        files_a = sorted(f.name for f in dir_a.glob("*.json"))
        files_b = sorted(f.name for f in dir_b.glob("*.json"))
        assert files_a == files_b

    def test_different_names_produce_different_slugs(self, tmp_path):
        # Collision guard: distinct names must produce distinct stub filenames,
        # otherwise the second write silently overwrites the first.
        expected = [{"name": "[N150] Alpha"}, {"name": "[N150] Beta"}]
        synthesize_missing_legs(tmp_path, expected, run_result="failure")
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 2
        assert len({f.name for f in files}) == 2

    def test_json_dict_warns_and_returns_zero(self, tmp_path, capsys):
        # JSON object instead of array — log a warning, don't crash.
        stats = synthesize_missing_legs(tmp_path, '{"name":"Alpha"}', run_result="failure")
        assert stats == {"infra_stubbed": 0}
        assert "must be a JSON array" in capsys.readouterr().err

    def test_json_scalar_warns_and_returns_zero(self, tmp_path, capsys):
        stats = synthesize_missing_legs(tmp_path, "42", run_result="failure")
        assert stats == {"infra_stubbed": 0}
        assert "must be a JSON array" in capsys.readouterr().err

    def test_list_with_non_dict_entries_skipped(self, tmp_path):
        # Strings / nulls in the list are skipped silently; dicts still process.
        expected = [{"name": "Alpha"}, "not-a-dict", None, {"name": "Beta"}]
        stats = synthesize_missing_legs(tmp_path, expected, run_result="failure")
        assert stats == {"infra_stubbed": 2}

    def test_empty_run_result_warns_and_returns_zero(self, tmp_path, capsys):
        # synthesize_missing_legs called directly with run_result="" must not
        # silently stub everything — warn and bail.
        stats = synthesize_missing_legs(tmp_path, [{"name": "Alpha"}], run_result="")
        assert stats == {"infra_stubbed": 0}
        assert "--run-result is empty" in capsys.readouterr().err
