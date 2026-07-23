# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

from ai_run_summary.models import ParsedJobSummary
from ai_run_summary.parse import dedup_latest_attempt, parse_json_summary, parse_summaries_dir


def _leg(name: str, job_id: str, status: str = "SUCCESS", run_attempt=None) -> ParsedJobSummary:
    return ParsedJobSummary(source_file=Path("x"), job_id=job_id, job_name=name, status=status, run_attempt=run_attempt)


class TestDedupLatestAttempt:
    def test_run_attempt_wins_over_job_id_order(self):
        # run_attempt=2 wins even though its check_run_id is lower.
        legs = [
            _leg("MLA", "200", "TIMEOUT", run_attempt=1),
            _leg("MLA", "100", "SUCCESS", run_attempt=2),
        ]
        out = dedup_latest_attempt(legs)
        assert len(out) == 1
        assert out[0].run_attempt == 2
        assert out[0].status == "SUCCESS"

    def test_falls_back_to_job_id_when_no_run_attempt(self):
        legs = [_leg("MLA", "100", "TIMEOUT"), _leg("MLA", "200", "SUCCESS")]
        out = dedup_latest_attempt(legs)
        assert len(out) == 1
        assert out[0].job_id == "200"
        assert out[0].status == "SUCCESS"

    def test_single_attempt_legs_untouched(self):
        legs = [_leg("A", "10"), _leg("B", "11")]
        out = dedup_latest_attempt(legs)
        assert {s.job_name for s in out} == {"A", "B"}

    def test_named_stub_without_job_id_is_retained(self):
        # An infra stub has a unique name and no job_id — kept via its name, not
        # via the passthrough branch.
        legs = [_leg("A", "10"), _leg("KV", "", "INFRA_FAILURE")]
        out = dedup_latest_attempt(legs)
        assert len(out) == 2
        assert any(s.job_name == "KV" and s.status == "INFRA_FAILURE" for s in out)

    def test_unnamed_entry_passes_through(self):
        # No job_name (local run without --job-name): can't key by leg, kept.
        legs = [_leg("A", "10"), _leg("", "")]
        out = dedup_latest_attempt(legs)
        assert len(out) == 2
        assert any(s.job_name == "" for s in out)

    def test_same_name_same_attempt_warns(self, capsys):
        # Two distinct legs sharing a name within one attempt is a misconfig, not
        # a re-run — collapsed with a warning rather than silently.
        legs = [_leg("dup", "10", run_attempt=1), _leg("dup", "11", run_attempt=1)]
        out = dedup_latest_attempt(legs)
        assert len(out) == 1
        assert "share job name 'dup'" in capsys.readouterr().err


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

    def test_log_complete_read_from_job(self, tmp_path):
        # ai_job_summary writes _job.log_complete as True/False/None.
        for value in (True, False, None):
            data = {"_job": {"status": "CRASHED", "log_complete": value}}
            f = tmp_path / f"t_{value}.json"
            f.write_text(json.dumps(data))
            assert parse_json_summary(f).log_complete is value

    def test_log_complete_defaults_none_when_absent(self, tmp_path):
        data = {"_job": {"status": "CRASHED"}}
        f = tmp_path / "t_absent.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).log_complete is None

    def test_run_attempt_coerced_to_int(self, tmp_path):
        # int passes through; a stringly-typed producer is coerced; junk → None.
        for raw, expected in ((2, 2), ("2", 2), ("x", None), (None, None)):
            data = {"_job": {"status": "SUCCESS", "run_attempt": raw}}
            f = tmp_path / f"ra_{raw}.json"
            f.write_text(json.dumps(data))
            assert parse_json_summary(f).run_attempt == expected

    def test_run_attempt_defaults_none_when_absent(self, tmp_path):
        data = {"_job": {"status": "SUCCESS"}}
        f = tmp_path / "ra_absent.json"
        f.write_text(json.dumps(data))
        assert parse_json_summary(f).run_attempt is None

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
