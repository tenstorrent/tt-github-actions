# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
import pytest
from unittest.mock import MagicMock
from benchmark import ShieldBenchmarkDataMapper, VllmBenchmarkDataMapper, CompleteBenchmarkRun


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


@pytest.mark.parametrize(
    "input_val, expected",
    [
        (None, None),
        (128, 128),
        ("256", 256),
        ("n/a", None),
        ("N/A", None),
        ("", None),
        (128.0, 128),
        (128.9, 128),
        (-128, -128),
        (-128.5, -128),
        ("-256", -256),
        (True, None),
        (False, None),
    ],
)
def test_coerce_optional_int(input_val, expected):
    result = CompleteBenchmarkRun.coerce_optional_int(input_val)
    assert result == expected


# --- VllmBenchmarkDataMapper tests ---

SAMPLE_VLLM_BENCH_OUTPUT = {
    "date": "20260318-154232",
    "model_id": "deepseek-ai/DeepSeek-R1-0528",
    "tokenizer_id": "deepseek-ai/DeepSeek-R1-0528",
    "num_prompts": 1000,
    "max_concurrency": 64,
    "duration": 0.883650948991999,
    "completed": 1000,
    "failed": 0,
    "total_input_tokens": 127000,
    "total_output_tokens": 127853,
    "request_throughput": 1131.668563408123,
    "output_throughput": 144687.22083741875,
    "total_token_throughput": 288409.1283902503,
    "max_output_tokens_per_s": 128853,
    "mean_ttft_ms": 49.234016701811925,
    "median_ttft_ms": 48.76174801029265,
    "std_ttft_ms": 5.58479576545678,
    "p99_ttft_ms": 77.79025232302956,
    "mean_tpot_ms": 0.010592051617808856,
    "median_tpot_ms": 0.0019246693077225852,
    "std_tpot_ms": 0.017113567565542293,
    "p99_tpot_ms": 0.06440074482793354,
    "mean_itl_ms": 0.010470757914358829,
    "median_itl_ms": 0.0017030397430062294,
    "std_itl_ms": 0.20523902317521459,
    "p99_itl_ms": 0.00332703348249197,
}


@pytest.fixture
def vllm_pipeline():
    p = MagicMock()
    p.pipeline_start_ts = "2026-03-18T15:00:00Z"
    p.pipeline_end_ts = "2026-03-18T16:00:00Z"
    p.project = "tt-inference-server"
    p.git_commit_hash = "abc123"
    p.pipeline_submission_ts = "2026-03-18T14:00:00Z"
    p.git_branch_name = "main"
    p.github_pipeline_id = 99999
    p.github_pipeline_link = "http://example.com/run/99999"
    p.git_author = "kosta"
    p.jobs = [
        MagicMock(
            github_job_id=1,
            job_start_ts="2026-03-18T15:10:00Z",
            job_end_ts="2026-03-18T15:50:00Z",
            docker_image="vllm_image",
            host_name="bench_host",
        )
    ]
    return p


@pytest.fixture
def vllm_mapper():
    return VllmBenchmarkDataMapper()


def test_vllm_produces_single_benchmark_run(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)


def test_vllm_run_type(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    assert result[0].run_type == "vllm_benchmark"


def test_vllm_model_name_strips_prefix(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    assert result[0].ml_model_name == "DeepSeek-R1-0528"


def test_vllm_model_name_no_prefix(vllm_mapper, vllm_pipeline):
    data = {**SAMPLE_VLLM_BENCH_OUTPUT, "model_id": "MyLocalModel"}
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, data)
    assert result[0].ml_model_name == "MyLocalModel"


def test_vllm_batch_size_is_max_concurrency(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    assert result[0].batch_size == 64


def test_vllm_config_params(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    config = result[0].config_params
    assert config["model_id"] == "deepseek-ai/DeepSeek-R1-0528"
    assert config["tokenizer_id"] == "deepseek-ai/DeepSeek-R1-0528"
    assert config["num_prompts"] == 1000
    assert config["max_concurrency"] == 64


def test_vllm_measurements_count(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    assert len(result[0].measurements) == len(VllmBenchmarkDataMapper.MEASUREMENT_KEYS)


def test_vllm_measurements_step_name(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    for m in result[0].measurements:
        assert m.step_name == "vllm_bench_serve"


def test_vllm_measurements_contain_key_metrics(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    names = {m.name for m in result[0].measurements}
    assert "mean_ttft_ms" in names
    assert "mean_tpot_ms" in names
    assert "mean_itl_ms" in names
    assert "request_throughput" in names
    assert "output_throughput" in names
    assert "total_token_throughput" in names
    assert "p99_ttft_ms" in names


def test_vllm_measurement_values(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    by_name = {m.name: m.value for m in result[0].measurements}
    assert by_name["mean_ttft_ms"] == pytest.approx(49.234016701811925)
    assert by_name["request_throughput"] == pytest.approx(1131.668563408123)
    assert by_name["completed"] == 1000
    assert by_name["failed"] == 0


def test_vllm_pipeline_metadata_propagated(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, SAMPLE_VLLM_BENCH_OUTPUT)
    run = result[0]
    assert run.git_repo_name == "tt-inference-server"
    assert run.git_commit_hash == "abc123"
    assert run.git_branch_name == "main"
    assert run.github_pipeline_id == 99999
    assert run.user_name == "kosta"
    assert run.device_hostname == "bench_host"


def test_vllm_no_job_found(vllm_mapper, vllm_pipeline):
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 999, SAMPLE_VLLM_BENCH_OUTPUT)
    assert result is None


def test_vllm_missing_metrics_still_works(vllm_mapper, vllm_pipeline):
    minimal = {
        "model_id": "test/model",
        "mean_ttft_ms": 50.0,
        "mean_tpot_ms": 1.0,
    }
    result = vllm_mapper.map_benchmark_data(vllm_pipeline, 1, minimal)
    assert len(result) == 1
    assert result[0].ml_model_name == "model"
    assert len(result[0].measurements) == 2
