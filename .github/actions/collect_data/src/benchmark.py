# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import math
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


def _load_model_spec_json(model_spec_path: pathlib.Path) -> dict | None:
    """
    HINT: Temporary helper method to load model_spec JSON for Shield benchmarks.
    model_spec is not yet integrated into the main report schema.
    Until then, we load it separately from here.
    """
    try:
        with open(model_spec_path) as model_spec_file:
            model_spec_data = json.load(model_spec_file)
            return model_spec_data
    except Exception as e:
        logger.error(f"Failed to load model_spec from {model_spec_path}: {e}")
        return None


def create_json_from_report(pipeline, workflow_outputs_dir) -> List[CompleteBenchmarkRun]:

    results = []
    reports = _get_model_reports(workflow_outputs_dir, pipeline.github_pipeline_id)
    for job_id, report_paths in reports.items():
        # First, find and load model_spec file if it exists
        model_spec_data = None
        model_spec_path = next((path for path in report_paths if "model_spec" in path.name), None)
        if model_spec_path:
            report_paths.remove(model_spec_path)
            model_spec_data = _load_model_spec_json(model_spec_path)
        if model_spec_data:
            logger.info(f"Loaded model_spec for job: {job_id} from {model_spec_path}")

        for report_path in report_paths:
            with open(report_path) as report_file:
                report_data = json.load(report_file)
                benchmark_data = _map_benchmark_data(pipeline, job_id, report_data, model_spec_data)
                if benchmark_data:
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

    def _get_job(self, pipeline, job_id):
        """
        Retrieves the job object from the pipeline using the job ID.
        """
        job = next((job for job in pipeline.jobs if job.github_job_id == job_id), None)
        if job is None:
            logger.error(f"No job found with github_job_id: {job_id}")
        return job

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
                except ValidationError as e:
                    logger.error(
                        f"Validation error while creating BenchmarkMeasurement for key '{key}' "
                        f"with value {data.get(key)!r}: {e}",
                        exc_info=True,
                    )
                except Exception as e:
                    logger.error(
                        f"Unexpected error while creating BenchmarkMeasurement for key '{key}' "
                        f"with value {data.get(key)!r}: {e}",
                        exc_info=True,
                    )
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
        model_type=None,
        input_seq_length=None,
        output_seq_length=None,
        dataset_name=None,
        batch_size=None,
        config_params=None,
        docker_image=None,
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
            docker_image=job.docker_image or docker_image,
            device_hostname=job.host_name,
            device_ip=None,
            device_info=(
                device_info if isinstance(device_info, dict) or device_info is None else {"device_name": device_info}
            ),
            ml_model_name=model_name,
            ml_model_type=model_type,
            num_layers=None,
            batch_size=batch_size,
            config_params=config_params,
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


class ForgeBenchmarkDataMapper(_BenchmarkDataMapper):
    def map_benchmark_data(self, pipeline, job_id, report_data, model_spec_data=None) -> CompleteBenchmarkRun | None:
        job = next((job for job in pipeline.jobs if job.github_job_id == job_id), None)
        if job is None:
            logger.error(f"No job found with github_job_id: {job_id}")
            return None

        try:
            benchmark_run = CompleteBenchmarkRun(
                run_start_ts=pipeline.pipeline_start_ts,
                run_end_ts=pipeline.pipeline_end_ts,
                run_type=report_data.get("run_type"),
                git_repo_name=report_data.get("project", pipeline.project),
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
    def map_benchmark_data(self, pipeline, job_id, report_data, model_spec_data=None) -> CompleteBenchmarkRun | None:
        """
        Maps benchmark and evaluation data from the report to CompleteBenchmarkRun objects.
        """
        job = self._get_job(pipeline, job_id)
        if job is None:
            return None

        metadata = report_data.get("metadata", {})

        try:
            benchmark_runs = self._process_benchmarks(
                pipeline,
                job,
                report_data.get("benchmarks", []),
                metadata,
                model_spec_data,
            )
            benchmark_summary_runs = self._process_benchmarks_summary(
                pipeline,
                job,
                report_data.get("benchmarks_summary", []),
                metadata,
                model_spec_data,
            )
            eval_runs = self._process_evals(
                pipeline,
                job,
                report_data.get("evals", []),
                metadata,
                model_spec_data,
            )
            return benchmark_runs + benchmark_summary_runs + eval_runs
        except ValidationError as e:
            failure_happened()
            logger.error(f"Validation error: {e}")
            return None

    def _format_model_name(self, benchmark):
        """
        Formats the model name by removing any prefix before '/' from model identifier.
        """
        model_name = benchmark.get("model_name")
        if model_name and "/" in model_name:
            model_name = model_name.split("/", 1)[1]
        return model_name

    def _process_benchmarks(self, pipeline, job, benchmarks, metadata=None, model_spec_data=None):
        """
        Processes benchmark entries and creates CompleteBenchmarkRun objects for each entry.
        """
        results = []
        for benchmark in benchmarks:
            if metadata:
                logger.debug(f"Processing benchmark with metadata included...")
                benchmark = {**benchmark, **metadata}  # metadata values take precedence
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
                    "num_requests",
                    "total_input_tokens",
                    "total_output_tokens",
                    "num_prompts",
                ],
            )

            model_name = self._format_model_name(benchmark)

            results.append(
                self._create_complete_benchmark_run(
                    pipeline=pipeline,
                    job=job,
                    data=benchmark,
                    run_type="benchmark",
                    measurements=measurements,
                    device_info=benchmark.get("device"),
                    model_name=model_name,
                    model_type=(model_spec_data.get("model_type") if model_spec_data else None),
                    input_seq_length=benchmark.get("input_sequence_length"),
                    output_seq_length=benchmark.get("output_sequence_length"),
                    dataset_name=benchmark.get("model_id", None),
                    batch_size=benchmark.get("max_con"),
                    config_params=model_spec_data,
                    docker_image=(model_spec_data or {}).get("docker_image") or job.docker_image,
                )
            )
        return results

    def _process_benchmarks_summary(self, pipeline, job, benchmarks_summary, metadata=None, model_spec_data=None):
        """
        Processes benchmark summary entries and creates CompleteBenchmarkRun objects for each entry.
        """
        results = []
        for benchmark in benchmarks_summary:
            if metadata:
                logger.debug(f"Processing benchmark summary with metadata included...")
                benchmark = {**benchmark, **metadata}  # metadata values take precedence
            measurements = self._create_measurements(
                job,
                "benchmark_summary",
                benchmark,
                [
                    "ttft",
                    "tput_user",
                    "tput",
                    "avg_gen_time",
                ],
            )

            target_checks = benchmark.get("target_checks", {})
            for target_name, target_data in target_checks.items():
                target_measurements = self._create_measurements(
                    job,
                    f"benchmark_summary_{target_name}",
                    target_data,
                    [
                        "ttft",
                        "ttft_ratio",
                        "ttft_check",
                        "tput_user",
                        "tput_user_ratio",
                        "tput_user_check",
                        "tput_check",
                        "avg_gen_time",
                        "avg_gen_time_ratio",
                        "avg_gen_time_check",
                    ],
                )
                measurements.extend(target_measurements)

            model_name = self._format_model_name(benchmark)

            # Extract device (should now be included in benchmarks_summary)
            device = benchmark.get("device", "unknown")

            results.append(
                self._create_complete_benchmark_run(
                    pipeline=pipeline,
                    job=job,
                    data=benchmark,
                    run_type="benchmark_summary",
                    measurements=measurements,
                    device_info=device,
                    model_name=model_name,
                    model_type=(model_spec_data.get("model_type") if model_spec_data else None),
                    input_seq_length=benchmark.get("isl"),
                    output_seq_length=benchmark.get("osl"),
                    dataset_name=model_name,
                    batch_size=benchmark.get("max_concurrency"),
                    config_params=model_spec_data,
                    docker_image=(model_spec_data or {}).get("docker_image") or job.docker_image,
                )
            )
        return results

    def _process_evals(self, pipeline, job, evals, metadata=None, model_spec_data=None):
        """
        Processes evaluation entries and creates CompleteBenchmarkRun objects for each entry.
        """
        results = []
        for eval_entry in evals:
            if metadata:
                logger.debug(f"Processing evals with metadata included...")
                eval_entry = {
                    **eval_entry,
                    **metadata,
                }  # metadata values take precedence
            measurements = self._create_measurements(
                job,
                "eval",
                eval_entry,
                [
                    "score",
                    "published_score",
                    "gpu_reference_score",
                    "accuracy_check",
                    "ratio_to_reference",
                    "ratio_to_published",
                    "average_clip",
                    "deviation_clip",
                    "fid_score",
                    "clip_accuracy_check_valid",
                    "fid_accuracy_check_valid",
                    "clip_accuracy_check_approx",
                    "fid_accuracy_check_approx",
                    "delta_clip",
                    "delta_fid",
                ],
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
                    model_type=(model_spec_data.get("model_type") if model_spec_data else None),
                    input_seq_length=None,
                    output_seq_length=None,
                    dataset_name=eval_entry.get("task_name"),
                    batch_size=None,
                    config_params=model_spec_data,
                    docker_image=(model_spec_data or {}).get("docker_image") or job.docker_image,
                )
            )
        return results


class VllmBenchmarkDataMapper(_BenchmarkDataMapper):
    """Maps flat vLLM bench serve JSON output to CompleteBenchmarkRun objects."""

    MEASUREMENT_KEYS = [
        "mean_ttft_ms",
        "median_ttft_ms",
        "std_ttft_ms",
        "p99_ttft_ms",
        "mean_tpot_ms",
        "median_tpot_ms",
        "std_tpot_ms",
        "p99_tpot_ms",
        "mean_itl_ms",
        "median_itl_ms",
        "std_itl_ms",
        "p99_itl_ms",
        "request_throughput",
        "output_throughput",
        "total_token_throughput",
        "max_output_tokens_per_s",
        "total_input_tokens",
        "total_output_tokens",
        "completed",
        "failed",
        "num_prompts",
        "duration",
    ]

    def map_benchmark_data(
        self, pipeline, job_id, report_data, model_spec_data=None
    ) -> List[CompleteBenchmarkRun] | None:
        job = self._get_job(pipeline, job_id)
        if job is None:
            return None

        try:
            model_id = report_data.get("model_id", "unknown")
            model_name = model_id.split("/", 1)[-1] if "/" in model_id else model_id

            measurements = self._create_measurements(job, "vllm_bench_serve", report_data, self.MEASUREMENT_KEYS)

            config_params = {
                "model_id": model_id,
                "tokenizer_id": report_data.get("tokenizer_id"),
                "num_prompts": report_data.get("num_prompts"),
                "max_concurrency": report_data.get("max_concurrency"),
            }

            return [
                self._create_complete_benchmark_run(
                    pipeline=pipeline,
                    job=job,
                    data=report_data,
                    run_type="vllm_benchmark",
                    measurements=measurements,
                    device_info=None,
                    model_name=model_name,
                    batch_size=report_data.get("max_concurrency"),
                    config_params=config_params,
                    input_seq_length=report_data.get("input_seq_len"),
                    output_seq_length=report_data.get("output_seq_len"),
                    docker_image=(model_spec_data or {}).get("docker_image") or job.docker_image,
                )
            ]
        except ValidationError as e:
            failure_happened()
            logger.error(f"Validation error: {e}")
            return None


class GuideLLMBenchmarkDataMapper(_BenchmarkDataMapper):
    @staticmethod
    def _safe_get(obj, path):
        cur = obj
        for k in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    @staticmethod
    def _flatten_numeric(obj, prefix=""):
        out = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_key = f"{prefix}_{k}" if prefix else str(k)
                out.update(GuideLLMBenchmarkDataMapper._flatten_numeric(v, new_key))
        if isinstance(obj, (int, float)) and math.isfinite(obj):
            out[prefix] = obj
        return out

    @staticmethod
    def _parse_data_spec(s):
        if not isinstance(s, str):
            return {}
        out = {}
        for tok in s.split(","):
            if "=" in tok:
                k, v = tok.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    @staticmethod
    def _redact_api_key(d):
        if not isinstance(d, dict):
            return d
        copy = dict(d)
        if "api_key" in copy:
            copy["api_key"] = "***REDACTED***"
        return copy

    def map_benchmark_data(
        self, pipeline, job_id, report_data, model_spec_data=None
    ) -> List[CompleteBenchmarkRun] | None:
        job = self._get_job(pipeline, job_id)
        if job is None:
            return None

        try:
            top_args = report_data.get("args") or {}
            metadata = report_data.get("metadata") or {}

            args_data = top_args.get("data") or []
            data_spec_str = args_data[0] if args_data and isinstance(args_data[0], str) else None
            data_spec = self._parse_data_spec(data_spec_str)

            prompt_tokens = int(data_spec["prompt_tokens"]) if "prompt_tokens" in data_spec else None
            output_tokens = int(data_spec["output_tokens"]) if "output_tokens" in data_spec else None

            top_args_redacted = dict(top_args)
            if "backend_kwargs" in top_args_redacted:
                top_args_redacted["backend_kwargs"] = self._redact_api_key(top_args_redacted.get("backend_kwargs"))

            dataset_name = top_args.get("processor", None)

            results = []
            for benchmark in report_data.get("benchmarks") or []:
                model_id = self._safe_get(benchmark, "config.backend.model") or ""
                model_name = model_id.split("/", 1)[-1] if "/" in model_id else model_id

                flat_metrics = {}
                flat_metrics.update(self._flatten_numeric(benchmark.get("metrics") or {}, "metrics"))
                flat_metrics.update(self._flatten_numeric(benchmark.get("scheduler_state") or {}, "scheduler_state"))
                flat_metrics.update(
                    self._flatten_numeric(benchmark.get("scheduler_metrics") or {}, "scheduler_metrics")
                )
                start = self._safe_get(benchmark, "scheduler_state.start_time")
                end = self._safe_get(benchmark, "scheduler_state.end_time")
                if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                    flat_metrics["duration"] = end - start

                measurements = self._create_measurements(
                    job, "guidellm_benchmark", flat_metrics, list(flat_metrics.keys())
                )

                bench_config = dict(self._safe_get(benchmark, "config") or {})
                if "backend" in bench_config:
                    bench_config["backend"] = self._redact_api_key(bench_config["backend"])

                config_params = {
                    "metadata": metadata,
                    "args": top_args_redacted,
                    "benchmark_id": benchmark.get("id_"),
                    "run_id": benchmark.get("run_id"),
                    "run_index": benchmark.get("run_index"),
                    "type_": benchmark.get("type_"),
                    "config": bench_config,
                    "scheduler_state_timestamps": {
                        k: self._safe_get(benchmark, f"scheduler_state.{k}")
                        for k in (
                            "start_time",
                            "end_time",
                            "start_requests_time",
                            "end_requests_time",
                            "end_queuing_time",
                            "end_processing_time",
                        )
                    },
                    "scheduler_metrics_timestamps": {
                        k: self._safe_get(benchmark, f"scheduler_metrics.{k}")
                        for k in (
                            "start_time",
                            "request_start_time",
                            "measure_start_time",
                            "measure_end_time",
                            "request_end_time",
                            "end_time",
                        )
                    },
                    "data_spec": data_spec_str,
                }

                results.append(
                    self._create_complete_benchmark_run(
                        pipeline=pipeline,
                        job=job,
                        data=benchmark,
                        run_type="guidellm_benchmark",
                        measurements=measurements,
                        device_info=None,
                        model_name=model_name,
                        batch_size=self._safe_get(benchmark, "config.strategy.max_concurrency"),
                        config_params=config_params,
                        input_seq_length=prompt_tokens,
                        output_seq_length=output_tokens,
                        dataset_name=dataset_name,
                        docker_image=(model_spec_data or {}).get("docker_image") or job.docker_image,
                    )
                )
            return results
        except ValidationError as e:
            failure_happened()
            logger.error(f"Validation error: {e}")
            return None


_REPORT_TYPE_MAPPERS = {
    ("tt-shield", "vllm_bench_serve"): VllmBenchmarkDataMapper,
    ("tt-inference-server", "vllm_bench_serve"): VllmBenchmarkDataMapper,
    ("tt-shield", "guidellm_benchmark"): GuideLLMBenchmarkDataMapper,
    ("tt-inference-server", "guidellm_benchmark"): GuideLLMBenchmarkDataMapper,
}


_PROJECT_MAPPERS = {
    "tt-forge-onnx": ForgeBenchmarkDataMapper,
    "tt-xla": ForgeBenchmarkDataMapper,
    "tt-forge": ForgeBenchmarkDataMapper,
    "tt-mlir": ForgeBenchmarkDataMapper,
    "tt-shield": ShieldBenchmarkDataMapper,
}


def _get_mapper(pipeline_project, report_data):
    report_type = report_data.get("report_type")
    mapper_cls = _REPORT_TYPE_MAPPERS.get((pipeline_project, report_type))
    if mapper_cls:
        return mapper_cls()
    mapper_cls = _PROJECT_MAPPERS.get(pipeline_project)
    if mapper_cls:
        return mapper_cls()
    raise ValueError(f"No mapper found for project {pipeline_project}!")


def _map_benchmark_data(pipeline, job_id, report_data, model_spec_data=None):
    mapper = _get_mapper(pipeline.project, report_data)
    return mapper.map_benchmark_data(pipeline, job_id, report_data, model_spec_data)
