# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import os
import enum
from datetime import datetime
from typing import Optional, Union

from loguru import logger


class InfraErrorV1(enum.Enum):
    GENERIC_SET_UP_FAILURE = enum.auto()


def parse_timestamp(timestamp):
    """
    Parse a timestamp string into a datetime object.
    Supports multiple formats with and without timezone and milliseconds.

    Supported formats:
    - "2024-12-23T02:56:37.036690+00:00"
    - "2024-12-23T02:56:37.036690"
    - "2024-12-23T02:56:37+00:00"
    - "2024-12-23T02:56:37"

    :param timestamp: Timestamp string to parse.
    :return: Parsed datetime object or None if parsing fails.
    """
    formats = [
        "%Y-%m-%dT%H:%M:%S.%f%z",  # With microseconds and timezone
        "%Y-%m-%dT%H:%M:%S.%f",  # With microseconds, no timezone
        "%Y-%m-%dT%H:%M:%S%z",  # No microseconds, with timezone
        "%Y-%m-%dT%H:%M:%S",  # No microseconds, no timezone
    ]

    for fmt in formats:
        try:
            return datetime.strptime(timestamp, fmt)
        except ValueError:
            continue  # Try the next format

    return None  # Return None if no format matches


def get_data_pipeline_datetime_from_datetime(requested_datetime):
    return requested_datetime.strftime("%Y-%m-%dT%H:%M:%S.%f%z")


def get_pipeline_row_from_github_info(github_runner_environment, github_pipeline_json, github_jobs_json):
    github_pipeline_id = github_pipeline_json["id"]
    pipeline_submission_ts = github_pipeline_json["created_at"]

    repository_url = github_pipeline_json["repository"]["html_url"]

    jobs = github_jobs_json["jobs"]
    jobs_start_times = list(map(lambda job_: parse_timestamp(job_["started_at"]), jobs))
    # We filter out jobs that started before because that means they're from a previous attempt for that pipeline
    eligible_jobs_start_times = list(
        filter(
            lambda job_start_time_: job_start_time_ >= parse_timestamp(pipeline_submission_ts),
            jobs_start_times,
        )
    )
    sorted_jobs_start_times = sorted(eligible_jobs_start_times)
    assert (
        sorted_jobs_start_times
    ), f"It seems that this pipeline does not have any jobs that started on or after the pipeline was submitted, which should be impossible. Please directly inspect the JSON objects"
    pipeline_start_ts = get_data_pipeline_datetime_from_datetime(sorted_jobs_start_times[0])

    pipeline_end_ts = github_pipeline_json["updated_at"]
    name = github_pipeline_json["name"]

    project = github_pipeline_json["repository"]["name"]

    trigger = github_runner_environment["github_event_name"]

    logger.warning("Using hardcoded value github for vcs_platform value")
    vcs_platform = "github"

    git_branch_name = github_pipeline_json["head_branch"]

    git_commit_hash = github_pipeline_json["head_sha"]

    git_author = github_pipeline_json["head_commit"]["author"]["name"]

    logger.warning("Using hardcoded value github_actions for orchestrator value")
    orchestrator = "github_actions"

    github_pipeline_link = github_pipeline_json["html_url"]

    return {
        "github_pipeline_id": github_pipeline_id,
        "repository_url": repository_url,
        "pipeline_submission_ts": pipeline_submission_ts,
        "pipeline_start_ts": pipeline_start_ts,
        "pipeline_end_ts": pipeline_end_ts,
        "name": name,
        "project": project,
        "trigger": trigger,
        "vcs_platform": vcs_platform,
        "git_branch_name": git_branch_name,
        "git_commit_hash": git_commit_hash,
        "git_author": git_author,
        "orchestrator": orchestrator,
        "github_pipeline_link": github_pipeline_link,
    }


def get_job_failure_signature_(github_job) -> Optional[Union[InfraErrorV1]]:
    if github_job["conclusion"] == "success":
        return None
    for step in github_job["steps"]:
        is_generic_setup_failure = (
            step["name"] == "Set up runner"
            and step["status"] in ("completed", "cancelled")
            and step["conclusion"] != "success"
            and step["started_at"] is not None
            and step["completed_at"] is None
        )
        if is_generic_setup_failure:
            return str(InfraErrorV1.GENERIC_SET_UP_FAILURE)
    return None


def get_job_row_from_github_job(github_job):
    github_job_id = github_job["id"]

    logger.info(f"Processing github job with ID {github_job_id}")

    host_name = github_job["runner_name"]

    labels = github_job["labels"]

    if not host_name:
        location = None
        host_name = None
    elif "GitHub Actions " in host_name:
        location = "github"
    else:
        location = "tt_cloud"

    os = None
    if location == "github":
        os_variants = ["ubuntu", "windows", "macos"]
        os = [label for label in labels if any(variant in label.lower() for variant in os_variants)][0]
        if os == "ubuntu-latest":
            logger.warning("Found ubuntu-latest, replacing with ubuntu-24.04 but may not be case for long")
            os = "ubuntu-24.04"

    if location == "tt_cloud":
        logger.warning("Assuming ubuntu-20.04 for tt cloud, but may not be the case soon")
        os = "ubuntu-20.04"

    name = github_job["name"]

    assert github_job["status"] == "completed", f"{github_job_id} is not completed"

    # Determine card type based on runner name
    runner_name = (github_job.get("runner_name") or "").upper()
    card_type = None
    for card in ["E150", "N150", "N300", "BH"]:
        if card in runner_name:
            card_type = card
            break

    job_submission_ts = github_job["created_at"]

    job_start_ts = github_job["started_at"]

    job_submission_ts_dt = parse_timestamp(job_submission_ts)
    job_start_ts_dt = parse_timestamp(job_start_ts)

    if job_submission_ts_dt > job_start_ts_dt:
        logger.warning(
            f"Job {github_job_id} seems to have a start time that's earlier than submission. Setting equal for data"
        )
        job_submission_ts = job_start_ts

    job_end_ts = github_job["completed_at"]

    job_success = github_job["conclusion"] == "success"
    job_status = str(github_job.get("conclusion", "unknown"))

    is_build_job = "build" in name or "build" in labels

    logger.warning("Returning None for job_matrix_config because difficult to get right now")
    job_matrix_config = None

    logger.warning("docker_image erroneously used in pipeline data model, but should be moved. Returning null")
    docker_image = None

    github_job_link = github_job["html_url"]

    failure_signature = get_job_failure_signature_(github_job)

    return {
        "github_job_id": github_job_id,
        "host_name": host_name,
        "card_type": card_type,
        "os": os,
        "location": location,
        "name": name,
        "job_submission_ts": job_submission_ts,
        "job_start_ts": job_start_ts,
        "job_end_ts": job_end_ts,
        "job_success": job_success,
        "job_status": job_status,
        "is_build_job": is_build_job,
        "job_matrix_config": job_matrix_config,
        "docker_image": docker_image,
        "github_job_link": github_job_link,
        "failure_signature": failure_signature,
    }


def get_job_rows_from_github_info(github_pipeline_json, github_jobs_json):
    return list(map(get_job_row_from_github_job, github_jobs_json["jobs"]))


def get_github_runner_environment():
    assert "GITHUB_EVENT_NAME" in os.environ
    github_event_name = os.environ["GITHUB_EVENT_NAME"]

    return {
        "github_event_name": github_event_name,
    }
