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
from abc import ABC, abstractmethod
from typing import List, Dict

"""
Generate benchmark data from perf reports.
"""


def create_json_from_report(pipeline, workflow_outputs_dir) -> List[CompleteBenchmarkRun]:

    results = []
    reports = _get_model_reports(workflow_outputs_dir, pipeline.github_pipeline_id)

    for job_id, report_paths in reports.items():
        for report_path in report_paths:
            with open(report_path) as report_file:
                report_data = json.load(report_file)
                benchmark_data = _map_benchmark_data(pipeline, job_id, report_data)
                if benchmark_data is not None:
                    results.extend(benchmark_data)
                    logger.info(f"Created benchmark data for job: {job_id} from report: {report_path}")
    return results


def get_benchmark_filename(report) -> str:
    ts = report.run_start_ts.strftime("%Y-%m-%dT%H:%M:%S%z")
    return f"benchmark_{report.github_pipeline_id}_{ts}.jsonl"


def _get_model_reports(workflow_outputs_dir, workflow_run_id: int) -> Dict[int, List[pathlib.Path]]:
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


class _BenchmarkDataMapper(ABC):
    @abstractmethod
    def map_benchmark_data(self, pipeline, job_id, report_data) -> CompleteBenchmarkRun | None:
        pass


class ForgeFeBenchmarkDataMapper(_BenchmarkDataMapper):
    def map_benchmark_data(self, pipeline, job_id, report_data) -> CompleteBenchmarkRun | None:
        job = next((job for job in pipeline.jobs if job.github_job_id == job_id), None)
        if job is None:
            logger.error(f"No job found with github_job_id: {job_id}")
            return None

        try:
            benchmark_run = CompleteBenchmarkRun(
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
            return [benchmark_run]
        except ValidationError as e:
            failure_happened()
            logger.error(f"Validation error: {e}")
            return None


class ShieldBenchmarkDataMapper(_BenchmarkDataMapper):
    def map_benchmark_data(self, pipeline, job_id, report_data) -> CompleteBenchmarkRun | None:
        """
        Maps benchmark and evaluation data from the report to CompleteBenchmarkRun objects.
        """
        job = self._get_job(pipeline, job_id)
        if job is None:
            return None

        try:
            benchmark_runs = self._process_benchmarks(pipeline, job, report_data.get("benchmarks", []))
            eval_runs = self._process_evals(pipeline, job, report_data.get("evals", []))
            return benchmark_runs + eval_runs
        except ValidationError as e:
            failure_happened()
            logger.error(f"Validation error: {e}")
            return None

    def _get_job(self, pipeline, job_id):
        """
        Retrieves the job object from the pipeline using the job ID.
        """
        job = next((job for job in pipeline.jobs if job.github_job_id == job_id), None)
        if job is None:
            logger.error(f"No job found with github_job_id: {job_id}")
        return job

    def _process_benchmarks(self, pipeline, job, benchmarks):
        """
        Processes benchmark entries and creates CompleteBenchmarkRun objects for each entry.
        """
        results = []
        for benchmark in benchmarks:
            measurements = self._create_measurements(
                job,
                "benchmark",
                benchmark,
                [
                    "mean_ttft_ms",
                    "std_ttft_ms",
                    "mean_tpot_ms",
                    "std_tpot_ms",
                    "mean_tps",
                    "std_tps",
                    "tps_decode_throughput",
                    "tps_prefill_throughput",
                    "mean_e2el_ms",
                    "request_throughput",
                ],
            )
            model_name = benchmark.get("model_id")
            if model_name and "/" in model_name:
                model_name = model_name.split("/", 1)[1]
            results.append(
                self._create_complete_benchmark_run(
                    pipeline=pipeline,
                    job=job,
                    data=benchmark,
                    run_type="benchmark",
                    measurements=measurements,
                    device_info=benchmark.get("device"),
                    model_name=model_name,
                    input_seq_length=benchmark.get("input_sequence_length"),
                    output_seq_length=benchmark.get("output_sequence_length"),
                    dataset_name=benchmark.get("model_id", None),
                    batch_size=benchmark.get("max_con"),
                )
            )
        return results

    def _process_evals(self, pipeline, job, evals):
        """
        Processes evaluation entries and creates CompleteBenchmarkRun objects for each entry.
        """
        results = []
        for eval_entry in evals:
            measurements = self._create_measurements(
                job, "eval", eval_entry, ["score", "published_score", "gpu_reference_score"]
            )
            results.append(
                self._create_complete_benchmark_run(
                    pipeline=pipeline,
                    job=job,
                    data=eval_entry,
                    run_type="eval",
                    measurements=measurements,
                    device_info=eval_entry.get("device"),
                    model_name=eval_entry.get("model"),
                    input_seq_length=None,
                    output_seq_length=None,
                    dataset_name=eval_entry.get("metadata", {}).get("dataset_path"),
                    batch_size=None,
                )
            )
        return results

    def _create_measurements(self, job, step_name, data, keys):
        """
        Creates BenchmarkMeasurement objects for the specified keys in the data.
        """
        measurements = []
        for key in keys:
            if key in data:
                try:
                    measurement = BenchmarkMeasurement(
                        step_start_ts=job.job_start_ts,
                        step_end_ts=job.job_end_ts,
                        iteration=1,
                        step_name=step_name,
                        step_warm_up_num_iterations=None,
                        name=key,
                        value=data.get(key),
                        target=None,
                        device_power=None,
                        device_temperature=None,
                    )
                    measurements.append(measurement)
                except Exception as e:
                    logger.error(f"Error constructing BenchmarkMeasurement for key: {key}, value: {data.get(key)}.")
        return measurements

    def _create_complete_benchmark_run(
        self,
        pipeline,
        job,
        data,
        run_type,
        measurements,
        device_info,
        model_name,
        input_seq_length=None,
        output_seq_length=None,
        dataset_name=None,
        batch_size=None,
    ):
        """
        Creates a CompleteBenchmarkRun object with the provided data and measurements.
        """
        return CompleteBenchmarkRun(
            run_start_ts=pipeline.pipeline_start_ts,
            run_end_ts=pipeline.pipeline_end_ts,
            run_type=run_type,
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
            device_ip=None,
            device_info={
                "device_name": device_info,
            },
            ml_model_name=model_name,
            ml_model_type=None,
            num_layers=None,
            batch_size=batch_size,
            config_params=None,
            precision=None,
            dataset_name=dataset_name,
            profiler_name=None,
            input_sequence_length=input_seq_length,
            output_sequence_length=output_seq_length,
            image_dimension=None,
            perf_analysis=None,
            training=False,
            measurements=measurements,
        )


def _map_benchmark_data(pipeline, job_id, report_data):
    if pipeline.project == "tt-forge-fe":
        mapper = ForgeFeBenchmarkDataMapper()
    elif pipeline.project == "tt-shield":
        mapper = ShieldBenchmarkDataMapper()
    else:
        logger.error(f"Unsupported project: {pipeline.project}")
        return None

    return mapper.map_benchmark_data(pipeline, job_id, report_data)
