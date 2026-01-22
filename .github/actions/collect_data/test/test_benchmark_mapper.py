# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
import pytest
from unittest.mock import MagicMock
from benchmark import ShieldBenchmarkDataMapper, CompleteBenchmarkRun


@pytest.fixture
def pipeline():
    pipeline = MagicMock()
    pipeline.pipeline_start_ts = "2025-04-14T07:00:00Z"
    pipeline.pipeline_end_ts = "2025-04-14T08:00:00Z"
    pipeline.project = "tt-shield"
    pipeline.git_commit_hash = "abc123"
    pipeline.pipeline_submission_ts = "2025-04-14T06:00:00Z"
    pipeline.git_branch_name = "main"
    pipeline.github_pipeline_id = 12345
    pipeline.github_pipeline_link = "http://example.com"
    pipeline.git_author = "test_user"
    pipeline.jobs = [
        MagicMock(
            github_job_id=1,
            job_start_ts="2025-04-14T07:10:00Z",
            job_end_ts="2025-04-14T07:20:00Z",
            docker_image="test_image",
            host_name="test_host",
        )
    ]
    return pipeline


@pytest.fixture
def mapper():
    return ShieldBenchmarkDataMapper()


def test_process_benchmarks(mapper, pipeline):
    report_data = {
        "benchmarks": [
            {
                "device": "test_device",
                "model_name": "test_model",
                "model_id": "test_model",
                "input_sequence_length": 128,
                "output_sequence_length": 128,
                "mean_ttft_ms": 100.0,
                "std_ttft_ms": 10.0,
            }
        ]
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert not isinstance(result[0].config_params, dict)
    assert len(result[0].measurements) == 2


def test_process_benchmarks_with_metadata(mapper, pipeline):
    report_data = {
        "metadata": {
            "report_id": "test_report",
            "model_name": "test_model",
            "model_id": "id_test_spec_test_model_test_device",
            "inference_engine": "vllm",
        },
        "benchmarks": [
            {
                "device": "test_device",
                "model_id": "test_model",
                "backend": "tt",
                "input_sequence_length": 128,
                "output_sequence_length": 128,
                "mean_ttft_ms": 100.0,
                "std_ttft_ms": 10.0,
            }
        ],
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert result[0].ml_model_name == "test_model"
    assert result[0].ml_model_type is None
    assert result[0].config_params is None
    assert len(result[0].measurements) == 2


def test_process_benchmarks_with_model_spec_data(mapper, pipeline):
    model_spec_data = {
        "model_id": "test_model",
        "impl": {
            "impl_id": "test_impl_id",
            "impl_name": "test_impl_name",
        },
        "inference_engine": "vllm",
        "device_type": "tt",
        "device_model_spec": {"device": "test_device", "max_concurrency": 1, "max_context": 2048},
        "env_vars": {"MESH_DEVICE": "test_mesh_device", "ARCH_NAME": "test_arch_name"},
    }
    report_data = {
        "benchmarks": [
            {
                "device": "test_device",
                "model_name": "test_model",
                "model_id": "test_model",
                "input_sequence_length": 128,
                "output_sequence_length": 128,
                "mean_ttft_ms": 100.0,
                "std_ttft_ms": 10.0,
            }
        ]
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data, model_spec_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert isinstance(result[0].config_params, dict)
    assert len(result[0].measurements) == 2


def test_process_evals(mapper, pipeline):
    report_data = {
        "evals": [
            {
                "device": "test_device",
                "model": "test_model",
                "score": 95.0,
                "published_score": 90.0,
                "gpu_reference_score": 85.0,
            }
        ]
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert result[0].config_params is None
    assert len(result[0].measurements) == 3


def test_process_evals_with_metadata(mapper, pipeline):
    report_data = {
        "metadata": {
            "report_id": "test_report",
            "model_name": "test_model",
            "model_id": "id_test_spec_test_model_test_device",
            "inference_engine": "vllm",
        },
        "evals": [
            {
                "device": "test_device",
                "model": "test_model",
                "score": 95.0,
                "published_score": 90.0,
                "gpu_reference_score": 85.0,
            }
        ],
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert result[0].ml_model_name == "test_model"
    assert result[0].ml_model_type is None
    assert result[0].config_params is None
    assert len(result[0].measurements) == 3


def test_process_evals_with_model_spec_data(mapper, pipeline):
    model_spec_data = {
        "model_id": "test_model",
        "impl": {
            "impl_id": "test_impl_id",
            "impl_name": "test_impl_name",
        },
        "inference_engine": "vllm",
        "device_type": "tt",
        "device_model_spec": {"device": "test_device", "max_concurrency": 1, "max_context": 2048},
        "env_vars": {"MESH_DEVICE": "test_mesh_device", "ARCH_NAME": "test_arch_name"},
    }
    report_data = {
        "evals": [
            {
                "device": "test_device",
                "model": "test_model",
                "score": 95.0,
                "published_score": 90.0,
                "gpu_reference_score": 85.0,
            }
        ]
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data, model_spec_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert isinstance(result[0].config_params, dict)
    assert len(result[0].measurements) == 3


def test_no_job_found(mapper, pipeline):
    result = mapper.map_benchmark_data(pipeline, 999, {})
    assert result is None


def test_format_model_name(mapper):
    benchmark = {"model_name": "Llama-3.2-1B"}
    result = mapper._format_model_name(benchmark)
    assert result == "Llama-3.2-1B"


def test_format_model_name_with_prefix(mapper):
    benchmark = {"model_name": "meta-llama/Llama-3.2-1B"}
    result = mapper._format_model_name(benchmark)
    assert result == "Llama-3.2-1B"


def test_format_model_name_none(mapper):
    benchmark = {"model_name": None}
    result = mapper._format_model_name(benchmark)
    assert result is None


def test_benchmarks_model_type_with_model_spec(mapper, pipeline):
    report_data = {"benchmarks": [{"model_name": "test_model"}]}
    model_spec_data = {"model_type": "LLM"}
    result = mapper.map_benchmark_data(pipeline, 1, report_data, model_spec_data)
    assert result[0].ml_model_type == "LLM"


def test_benchmarks_model_type_without_model_spec(mapper, pipeline):
    report_data = {"benchmarks": [{"model_name": "test_model"}]}
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert result[0].ml_model_type is None


def test_benchmark_summary_model_type_with_model_spec(mapper, pipeline):
    report_data = {"benchmarks_summary": [{"model_name": "test_model"}]}
    model_spec_data = {"model_type": "LLM"}
    result = mapper.map_benchmark_data(pipeline, 1, report_data, model_spec_data)
    assert result[0].ml_model_type == "LLM"


def test_benchmark_summary_model_type_without_model_spec(mapper, pipeline):
    report_data = {"benchmarks_summary": [{"model_name": "test_model"}]}
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert result[0].ml_model_type is None


def test_evals_model_type_with_model_spec(mapper, pipeline):
    report_data = {"evals": [{"model": "test_model"}]}
    model_spec_data = {"model_type": "LLM"}
    result = mapper.map_benchmark_data(pipeline, 1, report_data, model_spec_data)
    assert result[0].ml_model_type == "LLM"


def test_evals_model_type_without_model_spec(mapper, pipeline):
    report_data = {"evals": [{"model": "test_model"}]}
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert result[0].ml_model_type is None


def test_process_parameter_support_tests(mapper, pipeline):
    report_data = {
        "parameter_support_tests": [
            {
                "device": "test_device",
                "model": "test_model",
                "task_name": "test_task",
                "endpoint_url": "http://example.com/api",
                "results": {
                    "test_n": [{"status": "failed", "message": "n=10 not supported", "test_node_name": "test_n[2]"}],
                    "test_max_tokens": [
                        {
                            "status": "passed",
                            "message": "max_tokens=2048 supported",
                            "test_node_name": "test_max_tokens[3]",
                        }
                    ],
                },
            }
        ]
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert result[0].run_type == "parameter_support_test"
    assert result[0].config_params is None
    assert len(result[0].measurements) == 2
    measurements_by_name = {m.step_name: m for m in result[0].measurements}
    assert measurements_by_name["test_n"].value == 0.0
    assert measurements_by_name["test_max_tokens"].value == 1.0


def test_process_parameter_support_tests_with_metadata(mapper, pipeline):
    report_data = {
        "metadata": {
            "report_id": "test_report",
            "model_name": "test_model",
            "inference_engine": "vllm",
        },
        "parameter_support_tests": [
            {
                "device": "test_device",
                "model": "test_model",
                "task_name": "test_task",
                "endpoint_url": "http://example.com/api",
                "results": {
                    "test_n": [{"status": "failed", "message": "n=10 not supported", "test_node_name": "test_n[2]"}],
                    "test_max_tokens": [
                        {
                            "status": "passed",
                            "message": "max_tokens=2048 supported",
                            "test_node_name": "test_max_tokens[3]",
                        }
                    ],
                },
            }
        ],
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert result[0].run_type == "parameter_support_test"
    assert result[0].ml_model_type is None
    assert result[0].config_params is None
    assert len(result[0].measurements) == 2
    measurements_by_name = {m.step_name: m for m in result[0].measurements}
    assert measurements_by_name["test_n"].value == 0.0
    assert measurements_by_name["test_max_tokens"].value == 1.0


def test_process_parameter_support_tests_with_model_spec_data(mapper, pipeline):
    model_spec_data = {
        "model_id": "test_model",
        "model_type": "LLM",
        "impl": {
            "impl_id": "test_impl_id",
            "impl_name": "test_impl_name",
        },
        "inference_engine": "vllm",
        "device_type": "tt",
        "device_model_spec": {"device": "test_device", "max_concurrency": 1, "max_context": 2048},
        "env_vars": {"MESH_DEVICE": "test_mesh_device", "ARCH_NAME": "test_arch_name"},
    }
    report_data = {
        "parameter_support_tests": [
            {
                "device": "test_device",
                "model": "test_model",
                "task_name": "test_task",
                "endpoint_url": "http://example.com/api",
                "results": {
                    "test_n": [{"status": "failed", "message": "n=10 not supported", "test_node_name": "test_n[2]"}]
                },
            }
        ]
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data, model_spec_data)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)
    assert result[0].run_type == "parameter_support_test"
    assert result[0].ml_model_type == "LLM"
    assert isinstance(result[0].config_params, dict)
    assert len(result[0].measurements) == 1
    assert result[0].measurements[0].step_name == "test_n"
    assert result[0].measurements[0].value == 0.0
