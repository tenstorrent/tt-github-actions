# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for ai_run_summary.serialize."""

from pathlib import Path

from ai_run_summary.models import ParsedJobSummary
from ai_run_summary.serialize import build_run_json

META = {
    "run_id": "27490793160",
    "run_url": "https://github.com/org/repo/actions/runs/27490793160",
    "run_date": "2026-06-14",
    "run_attempt": 2,
}


def _job(name, status, **kw):
    return ParsedJobSummary(source_file=Path(f"{name}.json"), job_name=name, status=status, **kw)


class TestBuildRunJson:
    def test_meta_and_total_jobs(self):
        out = build_run_json([_job("a", "SUCCESS")], META)
        assert out["run_id"] == "27490793160"
        assert out["run_url"] == META["run_url"]
        assert out["run_date"] == "2026-06-14"
        assert out["run_attempt"] == 2
        assert out["total_jobs"] == 1

    def test_run_attempt_none_when_absent(self):
        out = build_run_json([_job("a", "SUCCESS")], {"run_id": "1"})
        assert out["run_attempt"] is None

    def test_three_way_grouping(self):
        summaries = [
            _job("ok", "SUCCESS", job_url="u/ok"),
            _job(
                "boom",
                "CRASHED",
                job_url="u/boom",
                category="cat",
                subcategory="sub",
                error_message="kaboom",
                root_cause="bad ptr",
            ),
            _job("late", "TIMEOUT", job_url="u/late"),
            _job(
                "ghost",
                "INFRA_FAILURE",
                category="infra:no_artifact",
                root_cause="no artifact",
            ),
        ]
        out = build_run_json(summaries, META)
        assert out["total_jobs"] == 4
        assert [j["job_name"] for j in out["succeeded"]] == ["ok"]
        assert {j["job_name"] for j in out["failed"]} == {"boom", "late"}
        assert [j["job_name"] for j in out["infra_failure"]] == ["ghost"]

    def test_succeeded_carries_name_and_url_only(self):
        out = build_run_json([_job("ok", "SUCCESS", job_url="u/ok", category="x")], META)
        assert out["succeeded"] == [{"job_name": "ok", "job_url": "u/ok"}]

    def test_failed_shape_keeps_precise_status_and_fields(self):
        summaries = [
            _job(
                "late",
                "TIMEOUT",
                job_url="u/late",
                category="c",
                subcategory="s",
                error_message="took too long",
                root_cause="hang",
                log_complete=False,
            )
        ]
        out = build_run_json(summaries, META)
        assert out["failed"] == [
            {
                "job_name": "late",
                "job_url": "u/late",
                "status": "TIMEOUT",
                "category": "c",
                "subcategory": "s",
                "error_message": "took too long",
                "root_cause": "hang",
                "log_complete": False,
            }
        ]

    def test_failed_log_complete_values_passthrough(self):
        # log_complete is tri-state: True (finished), False (truncated/killed),
        # None (no finish marker configured) -> JSON true/false/null.
        summaries = [
            _job("done", "FAILED", log_complete=True),
            _job("killed", "FAILED", log_complete=False),
            _job("unknown", "FAILED", log_complete=None),
        ]
        out = build_run_json(summaries, META)
        got = {r["job_name"]: r["log_complete"] for r in out["failed"]}
        assert got == {"done": True, "killed": False, "unknown": None}

    def test_infra_failure_shares_failed_shape(self):
        summaries = [
            _job(
                "ghost",
                "INFRA_FAILURE",
                category="infra:no_artifact",
                subcategory="",
                error_message="",
                root_cause="no artifact",
            )
        ]
        out = build_run_json(summaries, META)
        # Synthesized infra stubs carry no log, so log_complete is null.
        assert out["infra_failure"] == [
            {
                "job_name": "ghost",
                "job_url": "",
                "status": "INFRA_FAILURE",
                "category": "infra:no_artifact",
                "subcategory": "",
                "error_message": "",
                "root_cause": "no artifact",
                "log_complete": None,
            }
        ]

    def test_no_failures_run(self):
        summaries = [
            _job("a", "SUCCESS", job_url="u/a"),
            _job("b", "SUCCESS", job_url="u/b"),
        ]
        out = build_run_json(summaries, META)
        assert out["total_jobs"] == 2
        assert len(out["succeeded"]) == 2
        assert out["failed"] == []
        assert out["infra_failure"] == []

    def test_infra_stub_only_run(self):
        summaries = [
            _job(
                "g1",
                "INFRA_FAILURE",
                category="infra:no_artifact",
                root_cause="no artifact",
            ),
            _job(
                "g2",
                "INFRA_FAILURE",
                category="infra:no_artifact",
                root_cause="no artifact",
            ),
        ]
        out = build_run_json(summaries, META)
        assert out["succeeded"] == []
        assert out["failed"] == []
        assert len(out["infra_failure"]) == 2

    def test_empty_run(self):
        out = build_run_json([], META)
        assert out["total_jobs"] == 0
        assert out["succeeded"] == []
        assert out["failed"] == []
        assert out["infra_failure"] == []

    def test_lists_sorted_by_job_name(self):
        summaries = [
            _job("zeta", "SUCCESS", job_url="u/z"),
            _job("alpha", "SUCCESS", job_url="u/a"),
            _job("mike", "FAILED", job_url="u/m"),
            _job("bravo", "FAILED", job_url="u/b"),
        ]
        out = build_run_json(summaries, META)
        assert [j["job_name"] for j in out["succeeded"]] == ["alpha", "zeta"]
        assert [j["job_name"] for j in out["failed"]] == ["bravo", "mike"]
