# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Scoping, which keeps one invocation's report free of another's legs.

A reusable workflow invoked more than once per run has one run-summary job per
invocation, and every one of them downloads the same run-scoped artifacts.
"""

import json
from pathlib import Path

from ai_run_summary.cli import _received_names, _stub_infra, synthesize_missing_legs
from ai_run_summary.models import ParsedJobSummary
from ai_run_summary.parse import filter_by_scope, parse_summaries_dir


def _write(d: Path, stem: str, name: str, scope: str, status: str = "SUCCESS") -> None:
    (d / f"{stem}.json").write_text(json.dumps({"_job": {"name": name, "status": status, "scope": scope}}))


def _summary(scope: str, name: str = "leg") -> ParsedJobSummary:
    return ParsedJobSummary(source_file=Path(f"/x/{name}.md"), job_name=name, scope=scope)


class TestFilterByScope:
    def test_keeps_only_the_matching_invocation(self):
        got = filter_by_scope([_summary("u22", "a"), _summary("u24", "b"), _summary("u24", "c")], "u24")
        assert [s.job_name for s in got] == ["b", "c"]

    def test_unscoped_run_keeps_everything(self):
        summaries = [_summary("", "a"), _summary("", "b")]
        assert filter_by_scope(summaries, "") == summaries

    def test_unscoped_run_warns_when_the_legs_are_scoped(self, capsys):
        # Legs partitioned but the report not: silently mixes invocations.
        filter_by_scope([_summary("u22"), _summary("u24")], "")
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "u22" in err and "u24" in err

    def test_a_scoped_run_drops_unscoped_legs(self):
        assert filter_by_scope([_summary(""), _summary("u24")], "u24") == [_summary("u24")]


class TestScopedSynthesis:
    """Both invocations run the same matrix, so leg names are identical."""

    EXPECTED = json.dumps([{"name": "leg one"}, {"name": "leg two"}])

    def test_another_invocation_does_not_satisfy_our_expectation(self, tmp_path):
        _write(tmp_path, "ai_job_summary_u22_r5_a1_j1", "leg one", "u22")
        _write(tmp_path, "ai_job_summary_u22_r5_a1_j2", "leg two", "u22")
        # u24 produced nothing, so both its legs are missing despite u22's files.
        stats = synthesize_missing_legs(tmp_path, self.EXPECTED, "failure", scope="u24")
        assert stats["infra_stubbed"] == 2

    def test_our_own_artifact_does_satisfy_it(self, tmp_path):
        _write(tmp_path, "ai_job_summary_u24_r5_a1_j3", "leg one", "u24")
        stats = synthesize_missing_legs(tmp_path, self.EXPECTED, "failure", scope="u24")
        assert stats["infra_stubbed"] == 1

    def test_received_names_are_scope_local(self, tmp_path):
        _write(tmp_path, "ai_job_summary_u22_r5_a1_j1", "leg one", "u22")
        assert _received_names(tmp_path, "u22") == {"leg one"}
        assert _received_names(tmp_path, "u24") == set()

    def test_stubs_for_two_scopes_do_not_overwrite_each_other(self, tmp_path):
        _stub_infra(tmp_path, "leg one", "u22")
        _stub_infra(tmp_path, "leg one", "u24")
        assert len(list(tmp_path.glob("ai_job_summary_*.json"))) == 2

    def test_a_stub_carries_its_scope_so_the_filter_keeps_it(self, tmp_path):
        _stub_infra(tmp_path, "leg one", "u24")
        kept = filter_by_scope(parse_summaries_dir(tmp_path), "u24")
        assert [s.job_name for s in kept] == ["leg one"]
        assert filter_by_scope(parse_summaries_dir(tmp_path), "u22") == []
