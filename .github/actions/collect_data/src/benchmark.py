# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import os
import pathlib
import json
from loguru import logger
from pydantic import ValidationError
from pydantic_models import BenchmarkMeasurement, CompleteBenchmarkRun
from shared import failure_happened

"""
Generate benchmark data from perf reports.
"""


def create_json_from_report(pipeline, workflow_outputs_dir):
    results = []
    reports = _get_model_reports(workflow_outputs_dir, pipeline.github_pipeline_id)

    for job_id, report_paths in reports.items():
        for report_path in report_paths:
            with open(report_path) as report_file:
                report_data = json.load(report_file)
                benchmark_data = _map_benchmark_data(pipeline, job_id, report_data)
                if benchmark_data is not None:
                    results.append(benchmark_data)
                    logger.info(f"Created benchmark data for job: {job_id} model: {report_data['model']}")
    return results


def get_benchmark_filename(report):
    ts = report.run_start_ts.strftime("%Y-%m-%dT%H:%M:%S%z")
    return f"benchmark_{report.github_job_id}_{ts}.json"


def _get_model_reports(workflow_outputs_dir, workflow_run_id: int):
    """
    This function searches for perf reports in the artifacts directory
    and returns a mapping of job IDs to the paths of the perf reports.
    We expect that report filename is in format `<report_name>_<job_id>.json`.
    """
    job_paths_map = {}
    artifacts_dir = f"{workflow_outputs_dir}/{workflow_run_id}/artifacts"

    logger.info(f"Searching for perf reports in {artifacts_dir}")

    for root, _, files in os.walk(artifacts_dir):
        for file in files:
            if file.endswith(".json"):
                logger.debug(f"Found perf report {file}")
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


def _map_benchmark_data(pipeline, job_id, report_data):

    # get job information from pipeline
    job = next((job for job in pipeline.jobs if job.github_job_id == job_id), None)
    if job is None:
        logger.error(f"No job found with github_job_id: {job_id}")
        return None

    try:
        return CompleteBenchmarkRun(
            run_start_ts=pipeline.pipeline_start_ts,
            run_end_ts=pipeline.pipeline_end_ts,
            run_type=report_data.get("run_type"),
            git_repo_name=pipeline.project,
            git_commit_hash=pipeline.git_commit_hash,
            git_commit_ts=pipeline.pipeline_submission_ts,
            git_branch_name=pipeline.git_branch_name,
            github_pipeline_id=pipeline.github_pipeline_id,
            github_pipeline_link=pipeline.github_pipeline_link,
            github_job_id=job.github_job_id,
            user_name=pipeline.git_author,
            docker_image=job.docker_image,
            device_hostname=job.host_name,
            device_ip=report_data.get("device_ip", None),
            device_info=report_data.get("device_info"),
            ml_model_name=report_data.get("model"),
            ml_model_type=report_data.get("model_type"),
            num_layers=report_data.get("num_layers"),
            batch_size=report_data.get("batch_size", None),
            config_params=report_data.get("config"),
            precision=report_data.get("precision"),
            dataset_name=report_data.get("dataset_name"),
            profiler_name=report_data.get("profile_name"),
            input_sequence_length=report_data.get("input_sequence_length"),
            output_sequence_length=report_data.get("output_sequence_length"),
            image_dimension=report_data.get("image_dimension"),
            perf_analysis=report_data.get("perf_analysis"),
            training=report_data.get("training", False),
            measurements=[
                BenchmarkMeasurement(
                    step_start_ts=job.job_start_ts,
                    step_end_ts=job.job_end_ts,
                    iteration=measurement.get("iteration"),
                    step_name=measurement.get("step_name"),
                    step_warm_up_num_iterations=measurement.get("step_warm_up_num_iterations"),
                    name=measurement.get("measurement_name"),
                    value=measurement.get("value"),
                    target=measurement.get("target"),
                    device_power=measurement.get("device_power"),
                    device_temperature=measurement.get("device_temperature"),
                )
                for measurement in report_data.get("measurements", [])
            ],
        )
    except ValidationError as e:
        failure_happened()
        logger.error(f"Validation error: {e}")
        return None
