# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import os
import json
import pathlib
from loguru import logger
from datetime import timedelta
import random
from pydantic import ValidationError
from shared import failure_happened

from utils import (
    get_pipeline_row_from_github_info,
    get_job_rows_from_github_info,
    get_data_pipeline_datetime_from_datetime,
    parse_timestamp,
)
import pydantic_models
from test_parser import parse_file


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
    project = raw_pipeline["project"]

    jobs = []
    for raw_job in raw_jobs:
        tests = []
        github_job_id = raw_job["github_job_id"]
        job_name = raw_job.get("name", "")
        logger.info(f"Processing raw GitHub job {github_job_id} with name '{job_name}'")
        if github_job_id in github_job_id_to_test_reports:
            for test_report_path in github_job_id_to_test_reports[github_job_id]:
                logger.info(f"Processing test report {test_report_path}")
                tests_in_report = parse_file(
                    test_report_path, project=project, github_job_id=github_job_id, job_name=job_name
                )
                logger.info(f"Found {len(tests_in_report)} tests in report {test_report_path}")
                tests.extend(tests_in_report)
            logger.info(f"Found {len(tests)} tests total for job {github_job_id}")
        raw_job["job_start_ts"] = alter_time(raw_job["job_start_ts"])
        try:
            jobs.append(pydantic_models.Job(**raw_job, tests=tests))
        except ValidationError as e:
            failure_happened()
            logger.error(f"Failed to create job: {e}")
            logger.error(f"Job: {raw_job}")
            logger.error(f"Tests: {tests}")

    return pydantic_models.Pipeline(**raw_pipeline, jobs=jobs)


def get_github_job_id_to_test_reports(workflow_outputs_dir, workflow_run_id: int, extension=".xml"):
    """
    This function searches for test reports in the artifacts directory
    and returns a mapping of job IDs to the paths of the test reports.
    We expect that report filename is in format `<report_name>_<job_id>.xml`.
    """
    job_paths_map = {}
    artifacts_dir = f"{workflow_outputs_dir}/{workflow_run_id}/artifacts"

    logger.info(f"Searching for test reports in {artifacts_dir}")

    for root, _, files in os.walk(artifacts_dir):
        for file in files:
            if file.endswith(extension):
                logger.debug(f"Found test report {file}")
                file_path = pathlib.Path(root) / file
                filename = file_path.name
                try:
                    job_id = int(filename.split(".")[-2].split("_")[-1])
                except ValueError:
                    logger.warning(f"Could not extract job ID from {filename}")
                    continue
                report_paths = job_paths_map.get(job_id, [])
                report_paths.append(file_path)
                job_paths_map[job_id] = report_paths
    return job_paths_map


def alter_time(timestamp):
    # Workarpound for the fact that we don't have milliseconds in the timestamp
    # Add a random number of milliseconds to the timestamp to make it unique
    original_timestamp = parse_timestamp(timestamp)
    altered_time = original_timestamp + timedelta(milliseconds=random.randint(0, 999))
    altered_time_str = altered_time.isoformat(sep=" ", timespec="milliseconds")
    return altered_time_str
