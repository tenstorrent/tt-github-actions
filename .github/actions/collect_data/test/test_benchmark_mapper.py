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
    assert len(result[0].measurements) == 3


def test_no_job_found(mapper, pipeline):
    result = mapper.map_benchmark_data(pipeline, 999, {})
    assert result is None


def test_format_model_name_with_prefix(mapper):
    benchmark = {"model_id": "meta-llama/Llama-3.2-1B"}
    result = mapper._format_model_name(benchmark)
    assert result == "Llama-3.2-1B"


def test_format_model_name_none(mapper):
    benchmark = {"model_id": None}
    result = mapper._format_model_name(benchmark)
    assert result is None


def test_format_model_type_both_present(mapper):
    benchmark = {"inference_engine": "vllm", "backend": "tt"}
    result = mapper._format_model_type(benchmark)
    assert result == "vllm_tt"


def test_format_model_type_missing_inference_engine(mapper):
    benchmark = {"backend": "tt"}
    result = mapper._format_model_type(benchmark)
    assert result is None


def test_format_model_type_missing_backend(mapper):
    benchmark = {"inference_engine": "vllm"}
    result = mapper._format_model_type(benchmark)
    assert result is None


def test_format_model_type_both_missing(mapper):
    benchmark = {}
    result = mapper._format_model_type(benchmark)
    assert result is None


def test_format_model_type_backend_none(mapper):
    benchmark = {"inference_engine": "vllm", "backend": None}
    result = mapper._format_model_type(benchmark)
    assert result is None


def test_format_model_type_inference_engine_none(mapper):
    benchmark = {"inference_engine": None, "backend": "tt"}
    result = mapper._format_model_type(benchmark)
    assert result is None
