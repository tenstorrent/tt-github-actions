# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import os
import enum
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
import subprocess
from loguru import logger


class InfraErrorV1(enum.Enum):
    GENERIC_SET_UP_FAILURE = enum.auto()


def parse_timestamp(timestamp: str) -> Optional[datetime]:
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


def get_data_pipeline_datetime_from_datetime(requested_datetime: datetime) -> str:
    return requested_datetime.strftime("%Y-%m-%dT%H:%M:%S.%f%z")


def get_pipeline_row_from_github_info(
    github_runner_environment: Dict[str, Any],
    github_pipeline_json: Dict[str, Any],
    github_jobs_json: Dict[str, Any],
) -> Dict[str, Any]:
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


def get_job_failure_signature(github_job: Dict[str, Any]) -> Optional[Union[InfraErrorV1, str]]:
    if github_job["conclusion"] == "success":
        return None
    failed_steps = get_failed_steps(github_job)
    if failed_steps:
        return failed_steps[0]
    return None


def get_failed_steps(github_job: Dict[str, Any]) -> List[str]:
    """
    Find all steps with 'status': 'completed' and 'conclusion': 'failure'
    """
    failed_steps = []
    for step in github_job.get("steps", []):
        if step.get("status") == "completed" and step.get("conclusion") == "failure":
            failed_steps.append(step["name"])
    return failed_steps


def get_failure_description(github_job: Dict[str, Any], repository: str = None) -> Optional[str]:
    """
    Get failure description for a job by extracting error messages from logs
    of failed steps.
    """
    failed_steps = get_failed_steps(github_job)
    if not failed_steps:
        return None
    error_descriptions = ""
    if len(failed_steps) > 1:
        error_descriptions = f"Failed steps: {', '.join(step for step in failed_steps)}\n"
    job_id = github_job.get("id")
    # Try to get logs if possible
    try:
        logs = get_job_logs(repository, job_id)
        error_lines = extract_error_lines_from_logs(logs)
        if error_lines:
            # Limit to first 5 error lines to keep description concise
            error_descriptions += "\n".join(error_lines[:5])
        else:
            error_descriptions += "No specific error message found"
    except Exception as e:
        return error_descriptions + f"Error fetching logs: {str(e)}"
    return error_descriptions


def get_job_logs(repository: str, job_id: int) -> str:
    """
    Get logs for a specific job using GitHub CLI.
    """
    cmd = ["gh", "api", f"/repos/{repository}/actions/jobs/{job_id}/logs"]
    logger.info(f"Fetching logs for job {job_id} to inspect failures")
    logger.info(" ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.error(f"Error fetching logs for job {job_id}: {e}")
        raise


def extract_error_lines_from_logs(logs: str) -> List[str]:
    """
    Extract error messages from job logs, removing timestamps and keeping error messages.
    Lines longer than 200 characters are truncated with '...' appended.
    Ignores lines between ##[group] and ##[endgroup] markers.
    """
    error_lines = []
    error_markers = ["##[error]", "error:", "exception:", "failed"]
    max_length = 300
    in_group_section = False

    for line in logs.splitlines():
        # Check if we're entering or exiting a group section.
        if "##[group]" in line:
            in_group_section = True
            continue
        elif "##[endgroup]" in line:
            in_group_section = False
            continue

        # Skip processing lines within group sections
        if in_group_section:
            continue

        line_lower = line.lower()
        # Check if the line contains any error marker
        for marker in error_markers:
            if marker in line_lower:
                clean_line = line[29:] if len(line) > 29 else line
                clean_line = clean_line.replace("##[error]", "")
                # Truncate line if it's too long
                if len(clean_line) > max_length:
                    clean_line = clean_line[:max_length] + "..."
                logger.info(f"Error line: {clean_line}")
                error_lines.append(clean_line)
                break  # Once we find a marker, no need to check others

    return error_lines


def get_job_row_from_github_job(github_job: Dict[str, Any]) -> Dict[str, Any]:
    github_job_id = github_job.get("id")

    logger.info(f"Processing github job with ID {github_job_id}")

    host_name = github_job.get("runner_name")

    labels = github_job.get("labels", [])

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
        os = "ubuntu-20.04"

    name = github_job.get("name")

    assert github_job.get("status") == "completed", f"{github_job_id} is not completed"

    # Determine card type based on runner name
    runner_name = (github_job.get("runner_name") or "").upper()
    card_type = None
    for card in ["E150", "N150", "N300", "BH"]:
        if card in runner_name:
            card_type = card
            break

    job_success = github_job.get("conclusion") == "success"
    job_status = str(github_job.get("conclusion", "unknown"))

    job_submission_ts = github_job.get("created_at")
    job_start_ts = github_job.get("started_at")
    job_end_ts = github_job.get("completed_at")
    job_submission_ts_dt = parse_timestamp(job_submission_ts)
    job_start_ts_dt = parse_timestamp(job_start_ts)

    # make corrections to timestamps
    if job_submission_ts_dt > job_start_ts_dt:
        logger.warning(
            f"Job {github_job_id} seems to have a start time that's earlier than submission. Setting equal for data"
        )
        job_submission_ts = job_start_ts

    if job_status == "skipped":
        logger.warning(f"Job {github_job_id} is skipped, setting start time equal to submission time for data")
        job_start_ts = job_submission_ts

    is_build_job = "build" in name or "build" in labels

    job_matrix_config = None

    docker_image = None

    github_job_link = github_job.get("html_url")

    # Get the repository from github_job_link if available
    repository = None
    github_job_link_str = github_job.get("html_url", "")
    if github_job_link_str:
        # Extract repository from URL format like: https://github.com/owner/repo/actions/runs/...
        parts = github_job_link_str.split("/")
        if len(parts) >= 5 and parts[2] == "github.com":
            repository = f"{parts[3]}/{parts[4]}"

    failure_signature = None
    failure_description = None
    if job_status == "failure":
        failure_signature = get_job_failure_signature(github_job)
        failure_description = get_failure_description(github_job, repository)

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
        "failure_description": failure_description,
    }


def get_job_rows_from_github_info(
    github_pipeline_json: Dict[str, Any], github_jobs_json: Dict[str, Any]
) -> List[Dict[str, Any]]:
    return list(map(get_job_row_from_github_job, github_jobs_json.get("jobs")))


def get_github_runner_environment() -> Dict[str, str]:
    github_event_name = os.environ.get("GITHUB_EVENT_NAME", "test")

    return {
        "github_event_name": github_event_name,
    }
