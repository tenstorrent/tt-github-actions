# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from loguru import logger
from cicd import get_github_job_id_to_test_reports
from utils import get_data_pipeline_datetime_from_datetime
import json
from parsers.tt_torch_model_tests_parser import TTTorchModelTestsParser
from parsers.builder_pytest_parser import BuilderPytestParser
from typing import Optional


def should_use_builder_pytest_parser(test_report: str, job_name: Optional[str], git_branch: Optional[str]) -> bool:
    """
    Determine if BuilderPytestParser should be used based on job name and git branch.

    :param test_report: Filename of the test report
    :param job_name: Job name to check for 'builder' keyword.
    :param git_branch: Git branch name, must be 'main' for builder parsing.
    :return: True if BuilderPytestParser should be used, False otherwise.
    """

    file_name = str(test_report).lower()
    is_builder_job = "builder" in job_name.lower() or "_builder" in file_name

    if not file_name.endswith(".xml"):
        return False

    if not job_name or not git_branch:
        return False

    # Use BuilderPytestParser only for jobs with "builder" in the name on main branch
    if is_builder_job and git_branch == "main":
        logger.info(f"Should use BuilderPytestParser for builder job '{job_name}' on main branch")
        return True
    elif is_builder_job and git_branch != "main":
        logger.info(f"Skipping BuilderPytestParser for builder job '{job_name}' on branch '{git_branch}' (not main)")

    return False


def should_use_tt_torch_model_tests_parser(test_report: str) -> bool:
    """
    Determine if the `TTTorchModelTestsParser` should be used on `test_report`

    :param test_report: Filename of the test report
    """
    return str(test_report).endswith(".tar")


def create_optest_reports(pipeline, workflow_outputs_dir):
    reports = []

    # Create a mapping from job_id to job_name
    job_id_to_name = {j.github_job_id: j.name for j in pipeline.jobs}

    git_branch = getattr(pipeline, "git_branch_name", "")
    logger.info(f"Processing OpTest pipeline on branch: {git_branch}")

    # Search for reports with both `.tar` & `.xml` extensions.
    github_job_id_to_test_reports = get_github_job_id_to_test_reports(
        workflow_outputs_dir, pipeline.github_pipeline_id, [".tar", ".xml"]
    )

    if len(github_job_id_to_test_reports) == 0:
        logger.info(f"No test reports to parse, skipping...")
        return []

    for github_job_id, test_reports in github_job_id_to_test_reports.items():
        tests = []
        job_name = str(job_id_to_name.get(github_job_id, ""))

        for test_report in test_reports:

            # Select parser based on report name, job name and git branch
            if should_use_builder_pytest_parser(test_report, job_name, git_branch):
                parser = BuilderPytestParser()
                logger.info(f"Using BuilderPytestParser for job '{job_name}' on branch '{git_branch}'")
            elif should_use_tt_torch_model_tests_parser(test_report):
                parser = TTTorchModelTestsParser()
                logger.info(f"Using TTTorchModelTestsParser for job '{job_name}'")
            else:
                logger.info(f"No suitable parser found for {test_report}")
                continue
            try:
                parsed_tests = parser.parse(test_report, project=pipeline.project, github_job_id=github_job_id)
                tests.extend(parsed_tests)
            except Exception as e:
                logger.error(f"Failed to parse {test_report} with {type(parser)}: {e}")
        reports.append((github_job_id, tests))
    return reports


def get_optest_filename(pipeline, job_id):
    github_pipeline_start_ts = get_data_pipeline_datetime_from_datetime(pipeline.pipeline_start_ts)
    return f"github_job_{job_id}_{github_pipeline_start_ts}.json"
