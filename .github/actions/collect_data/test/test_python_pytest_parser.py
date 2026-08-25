# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from test_parser import parse_file

TAGGED_REPORT = "test/data/12084081698/artifacts/report_torch_33706643326.xml"
NON_PYTEST_REPORT = "test/data/12007373278/artifacts/test-reports-runner/report_33467916002.xml"


def test_parse_file_extracts_job_tags():
    tags = parse_file(TAGGED_REPORT).job_tags
    assert tags["frontend"]["frontend"] == "tt-xla"
    assert tags["query_params"]["filters"]["exclude_operators"] is False
    assert tags["query_params"]["filters"]["operators"] is None
    assert tags["query_params"]["filters"]["test_plan"] == ["GEN", "BASIC"]


def test_parse_file_no_job_tags_in_non_pytest_report():
    result = parse_file(NON_PYTEST_REPORT)
    assert result.tests
    assert result.job_tags is None


def test_parse_file_no_job_tags_without_testsuite_properties(tmp_path):
    report = tmp_path / "report_1.xml"
    report.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<testsuites>\n"
        '  <testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1" time="1.0" '
        'timestamp="2026-08-25T00:00:00">\n'
        '    <testcase classname="tests.test_a" name="test_a" time="1.0" />\n'
        "  </testsuite>\n"
        "</testsuites>\n"
    )
    result = parse_file(str(report))
    assert len(result.tests) == 1
    assert result.job_tags is None
