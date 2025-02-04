# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
import os
import pathlib
from loguru import logger
from cicd import get_github_job_id_to_test_reports
from utils import get_data_pipeline_datetime_from_datetime
from parsers.tt_torch_model_tests_parser import TTTorchModelTestsParser


def create_optest_report(pipeline, workflow_outputs_dir):
    github_job_id_to_test_reports = get_github_job_id_to_test_reports(
        workflow_outputs_dir, pipeline.github_pipeline_id, ".tar"
    )
    parser = TTTorchModelTestsParser()
    tests = []
    for github_job_id, test_reports in github_job_id_to_test_reports.items():
        for test_report in test_reports:
            print(test_report)
            tests = parser.parse(test_report, project=pipeline.project, github_job_id=github_job_id)
            tests.extend(tests)
    return tests


def get_optest_filename(pipeline):
    github_pipeline_start_ts = get_data_pipeline_datetime_from_datetime(pipeline.pipeline_start_ts)
    github_pipeline_id = pipeline.github_pipeline_id
    return f"optest_{github_pipeline_id}_{github_pipeline_start_ts}.json"
