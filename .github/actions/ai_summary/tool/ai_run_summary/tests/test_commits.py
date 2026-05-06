# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for ai_run_summary/commits.py."""

from unittest.mock import patch

from ai_run_summary.commits import RunCommits, fetch_run_commits, _find_resolve_shas_job


class TestRunCommits:
    def test_defaults_empty(self):
        c = RunCommits()
        assert c.tt_metal == ""
        assert c.inference_server == ""
        assert c.vllm == ""

    def test_fields_settable(self):
        c = RunCommits(tt_metal="abc", inference_server="def", vllm="ghi")
        assert c.tt_metal == "abc"
        assert c.inference_server == "def"
        assert c.vllm == "ghi"


class TestFindResolvesShasJob:
    def test_returns_job_id_when_found(self):
        with patch("ai_run_summary.commits._run_gh") as mock_gh:
            mock_gh.side_effect = ["42\n", ""]
            result = _find_resolve_shas_job("org/repo", 99)
            assert result == 42

    def test_returns_none_when_not_found(self):
        with patch("ai_run_summary.commits._run_gh") as mock_gh:
            # First call returns no job IDs, second call returns count < 100
            mock_gh.side_effect = ["", "5\n"]
            result = _find_resolve_shas_job("org/repo", 99)
            assert result is None

    def test_returns_none_on_invalid_job_id(self):
        with patch("ai_run_summary.commits._run_gh") as mock_gh:
            mock_gh.side_effect = ["not-a-number\n", ""]
            result = _find_resolve_shas_job("org/repo", 99)
            assert result is None


class TestFetchRunCommits:
    def test_returns_empty_when_no_job_found(self, capsys):
        with patch("ai_run_summary.commits._find_resolve_shas_job", return_value=None):
            result = fetch_run_commits(12345)
        assert result == RunCommits()
        assert "Warning" in capsys.readouterr().err

    def test_returns_empty_when_no_logs(self):
        with patch("ai_run_summary.commits._find_resolve_shas_job", return_value=42):
            with patch("ai_run_summary.commits._run_gh", return_value=""):
                result = fetch_run_commits(12345)
        assert result == RunCommits()

    def test_parses_three_shas_in_order(self):
        logs = (
            "2026-01-01T00:00:00Z Full sha: aabbccdd11223344aabbccdd11223344aabbccdd\n"
            "2026-01-01T00:00:01Z Full sha: 1122334455667788aabbccdd1122334455667788\n"
            "2026-01-01T00:00:02Z Full sha: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
        )
        with patch("ai_run_summary.commits._find_resolve_shas_job", return_value=42):
            with patch("ai_run_summary.commits._run_gh", return_value=logs):
                result = fetch_run_commits(12345)
        assert result.tt_metal == "aabbccdd11223344aabbccdd11223344aabbccdd"
        assert result.inference_server == "1122334455667788aabbccdd1122334455667788"
        assert result.vllm == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    def test_partial_shas_fills_available(self):
        logs = "2026-01-01T00:00:00Z Full sha: aabbccdd11223344aabbccdd11223344aabbccdd\n"
        with patch("ai_run_summary.commits._find_resolve_shas_job", return_value=42):
            with patch("ai_run_summary.commits._run_gh", return_value=logs):
                result = fetch_run_commits(12345)
        assert result.tt_metal == "aabbccdd11223344aabbccdd11223344aabbccdd"
        assert result.inference_server == ""
        assert result.vllm == ""

    def test_ignores_non_hex_sha_lines(self):
        logs = (
            "2026-01-01T00:00:00Z Full sha: not-a-valid-sha\n"
            "2026-01-01T00:00:01Z Full sha: aabbccdd11223344aabbccdd11223344aabbccdd\n"
        )
        with patch("ai_run_summary.commits._find_resolve_shas_job", return_value=42):
            with patch("ai_run_summary.commits._run_gh", return_value=logs):
                result = fetch_run_commits(12345)
        assert result.tt_metal == "aabbccdd11223344aabbccdd11223344aabbccdd"
        assert result.inference_server == ""
