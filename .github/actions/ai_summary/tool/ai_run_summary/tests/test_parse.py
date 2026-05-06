# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
import json
from ai_run_summary.parse import parse_json_summary, parse_summaries_dir


class TestParseJsonSummary:
    def test_basic_fields(self, tmp_path):
        """Minimal valid JSON produces correct ParsedJobSummary."""
        data = {
            "category": "tt-metal:dispatch",
            "root_cause": "timeout",
            "_job": {"name": "my-job", "url": "http://example.com", "status": "CRASHED"},
        }
        f = tmp_path / "test.json"
        f.write_text(json.dumps(data))
        result = parse_json_summary(f)
        assert result is not None
        assert result.job_name == "my-job"
        assert result.job_url == "http://example.com"
        assert result.status == "CRASHED"
        assert result.category == "tt-metal:dispatch"
        assert result.root_cause == "timeout"

    def test_status_from_job_field(self, tmp_path):
        data = {"_job": {"status": "CRASHED"}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).status == "CRASHED"

    def test_status_with_suffix(self, tmp_path):
        data = {"_job": {"status": "TESTS FAILED (3 failed)"}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).status == "TESTS_FAILED"

    def test_status_infra_failure(self, tmp_path):
        data = {"_job": {"status": "INFRA_FAILURE"}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).status == "INFRA_FAILURE"

    def test_missing_status_collapses_to_failed(self, tmp_path):
        # UNKNOWN is no longer a valid status; missing status becomes FAILED.
        data = {"_job": {}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).status == "FAILED"

    def test_job_id_extracted_from_url(self, tmp_path):
        data = {"_job": {"url": "https://github.com/org/repo/actions/runs/1/job/99"}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).job_id == "99"

    def test_job_id_empty_when_no_job_segment(self, tmp_path):
        data = {"_job": {"url": "https://github.com/org/repo/actions/runs/1"}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).job_id == ""

    def test_job_id_empty_when_no_url(self, tmp_path):
        data = {"_job": {}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).job_id == ""

    def test_job_id_strips_query_string(self, tmp_path):
        data = {"_job": {"url": "https://github.com/org/repo/actions/runs/1/job/99?attempt=2"}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).job_id == "99"

    def test_failed_tests_from_job(self, tmp_path):
        data = {"failed_tests": ["old"], "_job": {"failed_tests": ["new1", "new2"]}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).failed_tests == ["new1", "new2"]

    def test_failed_tests_fallback(self, tmp_path):
        data = {"failed_tests": ["top_level"], "_job": {}}
        f = tmp_path / "t.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).failed_tests == ["top_level"]

    def test_invalid_json_returns_none(self, tmp_path, capsys):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        assert parse_json_summary(f) is None
        assert "Warning" in capsys.readouterr().err

    def test_file_not_found_returns_none(self, tmp_path, capsys):
        assert parse_json_summary(tmp_path / "nope.json") is None
        assert "Warning" in capsys.readouterr().err

    def test_markdown_sidecar_loaded(self, tmp_path):
        data = {"_job": {"status": "CRASHED"}}
        f = tmp_path / "ai_job_summary_123.json"
        f.write_text(json.dumps(data))
        md_path = tmp_path / "ai_job_summary_123.md"
        md_path.write_text("## Summary\nSome details here.")
        result = parse_json_summary(f)
        assert result is not None
        assert result.markdown == "## Summary\nSome details here."

    def test_markdown_sidecar_empty_when_missing(self, tmp_path):
        data = {"_job": {"status": "SUCCESS"}}
        f = tmp_path / "ai_job_summary_456.json"
        f.write_text(json.dumps(data))
        result = parse_json_summary(f)
        assert result is not None
        assert result.markdown == ""


class TestParseSummariesDir:
    def test_empty_dir(self, tmp_path):
        assert parse_summaries_dir(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        assert parse_summaries_dir(tmp_path / "nope") == []

    def test_json_files_parsed(self, sample_summaries_dir):
        results = parse_summaries_dir(sample_summaries_dir)
        assert len(results) == 6

    def test_ignores_non_json(self, tmp_path):
        (tmp_path / "readme.md").write_text("# not a summary")
        (tmp_path / "data.txt").write_text("nope")
        (tmp_path / "valid.json").write_text(json.dumps({"_job": {"status": "SUCCESS"}}))
        results = parse_summaries_dir(tmp_path)
        assert len(results) == 1

    def test_skips_invalid_warns(self, tmp_path, capsys):
        (tmp_path / "good.json").write_text(json.dumps({"_job": {"status": "SUCCESS"}}))
        (tmp_path / "bad.json").write_text("not json")
        results = parse_summaries_dir(tmp_path)
        assert len(results) == 1
        assert "Warning" in capsys.readouterr().err
