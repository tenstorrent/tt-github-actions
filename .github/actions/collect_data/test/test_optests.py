# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0


import pytest
from unittest.mock import MagicMock
from typing import Optional
from optests import should_use_builder_pytest_parser, create_optest_reports


@pytest.fixture
def pipeline():
    pipeline = MagicMock()
    pipeline.git_branch_name = "main"
    pipeline.github_pipeline_id = "17415392798"
    pipeline.jobs = [MagicMock(github_job_id=49443670279, name="test-reports-builder-n150-tracy-silicon-49443670279")]
    return pipeline


@pytest.fixture()
def workflow_outputs_dir() -> str:
    return "test/data/"


def test_report_discovery(pipeline, workflow_outputs_dir: str) -> None:
    reports = create_optest_reports(pipeline, workflow_outputs_dir)
    assert len(reports) == 1


@pytest.mark.parametrize(
    "report_name,job_name,branch,result",
    [
        ("report.xml", "builder", "main", True),
        ("report.tar", "builder", "main", False),
        ("blah_blah.xml", "blah_blah_builder_blah_blah", "main", True),
        ("report.xml", "other_job_name", "main", False),
        ("report.xml", None, None, False),
        ("report.xml", "builder", "feature_branch", False),
        ("report.xml", "other_job_name", "feature_branch", False),
        ("report_builder.xml", "other_job_name", "main", True),
        ("report_builder.xml", "other_job_name", "feature_branch", False),
    ],
)
def test_should_use_builder_parser(
    report_name: str, job_name: Optional[str], branch: Optional[str], result: bool
) -> None:
    assert should_use_builder_pytest_parser(report_name, job_name, branch) == result
