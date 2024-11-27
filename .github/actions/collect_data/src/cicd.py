# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
import json
from loguru import logger
from datetime import datetime, timedelta
import random

from utils import (
    get_pipeline_row_from_github_info,
    get_job_rows_from_github_info,
    get_data_pipeline_datetime_from_datetime,
    get_datetime_from_github_datetime,
)
from workflows import (
    get_github_job_id_to_test_reports,
    get_tests_from_test_report_path,
)
import pydantic_models


def get_cicd_json_filename(pipeline):
    github_pipeline_start_ts = get_data_pipeline_datetime_from_datetime(pipeline.pipeline_start_ts)
    github_pipeline_id = pipeline.github_pipeline_id
    cicd_json_filename = f"pipeline_{github_pipeline_id}_{github_pipeline_start_ts}.json"
    return cicd_json_filename


def create_cicd_json_for_data_analysis(
    workflow_outputs_dir,
    github_runner_environment,
    github_pipeline_json_filename,
    github_jobs_json_filename,
):
    logger.info(f"Load pipeline info from: {github_pipeline_json_filename}")
    with open(github_pipeline_json_filename) as github_pipeline_json_file:
        github_pipeline_json = json.load(github_pipeline_json_file)

    logger.info(f"Load jobs info from: {github_jobs_json_filename}")
    with open(github_jobs_json_filename) as github_jobs_json_file:
        github_jobs_json = json.load(github_jobs_json_file)

    raw_pipeline = get_pipeline_row_from_github_info(github_runner_environment, github_pipeline_json, github_jobs_json)
    raw_jobs = get_job_rows_from_github_info(github_pipeline_json, github_jobs_json)
    github_pipeline_id = raw_pipeline["github_pipeline_id"]
    github_job_id_to_test_reports = get_github_job_id_to_test_reports(workflow_outputs_dir, github_pipeline_id)

    jobs = []
    for raw_job in raw_jobs:
        tests = []
        github_job_id = raw_job["github_job_id"]
        logger.info(f"Processing raw GitHub job {github_job_id}")
        if github_job_id in github_job_id_to_test_reports:
            for test_report_path in github_job_id_to_test_reports[github_job_id]:
                logger.info(f"Processing test report {test_report_path}")
                tests_in_report = get_tests_from_test_report_path(test_report_path)
                logger.info(f"Found {len(tests_in_report)} tests in report {test_report_path}")
                tests.extend(tests_in_report)
            logger.info(f"Found {len(tests)} tests total for job {github_job_id}")
        raw_job["job_start_ts"] = alter_time(raw_job["job_start_ts"])
        jobs.append(pydantic_models.Job(**raw_job, tests=tests))

    return pydantic_models.Pipeline(**raw_pipeline, jobs=jobs)


def alter_time(timestamp):
    # Workarpound for the fact that we don't have milliseconds in the timestamp
    # Add a random number of milliseconds to the timestamp to make it unique
    original_timestamp = get_datetime_from_github_datetime(timestamp)
    altered_time = original_timestamp + timedelta(milliseconds=random.randint(0, 999))
    altered_time_str = altered_time.isoformat(sep=" ", timespec="milliseconds")
    return altered_time_str
