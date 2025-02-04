# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from loguru import logger
from cicd import get_github_job_id_to_test_reports
from utils import get_data_pipeline_datetime_from_datetime
from parsers.tt_torch_model_tests_parser import TTTorchModelTestsParser


def create_optest_reports(pipeline, workflow_outputs_dir):
    reports = []
    github_job_id_to_test_reports = get_github_job_id_to_test_reports(
        workflow_outputs_dir, pipeline.github_pipeline_id, ".tar"
    )
    parser = TTTorchModelTestsParser()

    for github_job_id, test_reports in github_job_id_to_test_reports.items():
        tests = []
        for test_report in test_reports:
            tests = parser.parse(test_report, project=pipeline.project, github_job_id=github_job_id)
            tests.extend(tests)
        reports.append((github_job_id, tests))
    return reports


def get_optest_filename(pipeline, job_id):
    github_pipeline_start_ts = get_data_pipeline_datetime_from_datetime(pipeline.pipeline_start_ts)
    return f"github_job_{job_id}_{github_pipeline_start_ts}.json"
