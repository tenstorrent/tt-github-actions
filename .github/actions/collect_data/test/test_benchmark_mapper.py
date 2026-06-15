# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
import pytest
from unittest.mock import MagicMock
from benchmark import (
    ShieldBenchmarkDataMapper,
    VllmBenchmarkDataMapper,
    GuideLLMBenchmarkDataMapper,
    CompleteBenchmarkRun,
)


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


# --- Whitelist coverage tests ---
#
# These tests pin down which scalar keys we ingest from each location in the
# Shield report JSON. Drift is caught by the metric-drift script in tt-shield
# (which AST-parses these whitelists); these tests make sure each whitelist
# actually covers what the producer emits for every model family we ship.
#
# Locations:
#   - benchmarks[*]                          → step_name='benchmark'
#   - benchmarks_summary[*]                  → step_name='benchmark_summary'
#   - benchmarks_summary[*].target_checks.*  → step_name='benchmark_summary_{tier}'
#   - evals[*]                               → step_name='eval'

# Keys observed in production across LLM, image/video, audio, embedding models
# (sourced from a 109-job nightly run analysis — see PR description).
_BENCHMARK_KEYS_ALL_FAMILIES = {
    # LLM
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
    "num_prompts",
    "total_input_tokens",
    "total_output_tokens",
    "total_token_throughput",
    # Image / video / diffusion
    "mean_latency_ms",
    "p50_latency_ms",
    "p90_latency_ms",
    "p95_latency_ms",
    "throughput_rps",
    "inference_steps_per_second",
    "num_inference_steps",
    "performance_check",
    # Audio (Whisper / speecht5)
    "rtr",
    "wer",
    # Embedding
    "embedding_dimension",
}

_BENCHMARK_SUMMARY_OUTER_KEYS = {
    # LLM
    "ttft",
    "tput_user",
    "tput",
    "avg_gen_time",
    "num_requests",
    # Image / video
    "latency",
    "inference_steps_per_second",
    "num_inference_steps",
    # Embedding
    "e2el_ms",
    "tput_prefill",
    # Audio
    "latency_p90",
    "latency_p95",
    "rtr",
}

_TARGET_CHECKS_KEYS = {
    # LLM
    "ttft",
    "ttft_ratio",
    "ttft_check",
    "tput_user",
    "tput_user_ratio",
    "tput_user_check",
    "tput",
    "tput_ratio",
    "tput_check",
    "avg_gen_time",
    "avg_gen_time_ratio",
    "avg_gen_time_check",
    # Media / image / video / whisper
    "latency",
    "latency_ratio",
    "latency_check",
    # Embedding
    "e2el_ms",
    "e2el_ms_ratio",
    "e2el_ms_check",
    "tput_prefill",
    "tput_prefill_ratio",
    "tput_prefill_check",
    # Audio (speecht5_tts)
    "rtr_check",
}

_EVAL_KEYS = {
    # Existing eval scores
    "score",
    "published_score",
    "gpu_reference_score",
    "ratio_to_reference",
    "ratio_to_published",
    "accuracy_check",
    # Image quality
    "average_clip",
    "deviation_clip",
    "deviation_clip_score",
    "fid_score",
    "clip_accuracy_check_valid",
    "fid_accuracy_check_valid",
    "clip_accuracy_check_approx",
    "fid_accuracy_check_approx",
    "delta_clip",
    "delta_fid",
    "max_clip",
    "min_clip",
    "clip_standard_deviation",
    "fvd",
    "fvmd",
    "performance_check",
    "tolerance",
    "num_inference_steps",
    "num_prompts",
    # Audio
    "latency_p50",
    "latency_p90",
    "latency_p95",
    "rtr",
    "throughput_rps",
    "tput_user",
    # Classification
    "correct",
    "total",
    "mismatches_count",
    # Embedding (sentence similarity)
    "cosine_pearson",
    "cosine_spearman",
    "euclidean_pearson",
    "euclidean_spearman",
    "manhattan_pearson",
    "manhattan_spearman",
    "pearson",
    "spearman",
    "main_score",
}


def _measurement_names(result, step_name):
    return {m.name for r in result for m in r.measurements if m.step_name == step_name}


def test_benchmarks_whitelist_covers_all_model_families(mapper, pipeline):
    """benchmarks[*] must ingest every scalar metric observed across LLM, image,
    audio, and embedding models. Producer drift would be caught by the
    tt-shield drift script; this test pins down current coverage."""
    benchmark = {k: 1.0 for k in _BENCHMARK_KEYS_ALL_FAMILIES}
    benchmark.update({"model_name": "test", "device": "test"})
    result = mapper.map_benchmark_data(pipeline, 1, {"benchmarks": [benchmark]})
    got = _measurement_names(result, "benchmark")
    missing = _BENCHMARK_KEYS_ALL_FAMILIES - got
    assert not missing, f"benchmarks[*] whitelist missing keys: {sorted(missing)}"


def test_benchmarks_summary_outer_whitelist_covers_all_model_families(mapper, pipeline):
    """benchmarks_summary[*] (outer, sibling of target_checks) must ingest the
    scalar throughput / latency metrics each model family emits there."""
    bs = {k: 1.0 for k in _BENCHMARK_SUMMARY_OUTER_KEYS}
    bs.update({"model_name": "test", "device": "test"})
    result = mapper.map_benchmark_data(pipeline, 1, {"benchmarks_summary": [bs]})
    got = _measurement_names(result, "benchmark_summary")
    missing = _BENCHMARK_SUMMARY_OUTER_KEYS - got
    assert not missing, f"benchmarks_summary[*] whitelist missing keys: {sorted(missing)}"


def test_target_checks_whitelist_covers_all_check_families(mapper, pipeline):
    """benchmarks_summary[*].target_checks.<tier> must ingest every <base>,
    <base>_ratio and <base>_check the producer emits across all model
    families. This is the whitelist that previously dropped whisper's
    latency_check — regression target for the original bug."""
    target_data = {k: 1.0 for k in _TARGET_CHECKS_KEYS}
    result = mapper.map_benchmark_data(
        pipeline,
        1,
        {"benchmarks_summary": [{"model_name": "test", "target_checks": {"functional": target_data}}]},
    )
    got = _measurement_names(result, "benchmark_summary_functional")
    missing = _TARGET_CHECKS_KEYS - got
    assert not missing, f"target_checks whitelist missing keys: {sorted(missing)}"


def test_evals_whitelist_covers_all_eval_metrics(mapper, pipeline):
    """evals[*] must ingest the scalar eval metrics each model family emits —
    LLM accuracy, image quality (FID/CLIP/FVD/FVMD), classification
    (correct/total), embedding similarity (cosine/euclidean/manhattan), and
    audio (rtr / latency_p*)."""
    eval_entry = {k: 1.0 for k in _EVAL_KEYS}
    eval_entry.update({"model": "test", "device": "test"})
    result = mapper.map_benchmark_data(pipeline, 1, {"evals": [eval_entry]})
    got = _measurement_names(result, "eval")
    missing = _EVAL_KEYS - got
    assert not missing, f"evals[*] whitelist missing keys: {sorted(missing)}"


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


# --- GuideLLMBenchmarkDataMapper tests ---

SAMPLE_GUIDELLM_OUTPUT = {
    "metadata": {
        "version": 1,
        "guidellm_version": "0.6.0",
        "python_version": "3.10.19",
        "platform": "Linux",
    },
    "args": {
        "data": [
            "turns=3,prompt_tokens=400,prompt_tokens_stdev=150,prompt_tokens_min=50,"
            "prompt_tokens_max=1500,output_tokens=3000,output_tokens_stdev=120,"
            "output_tokens_min=1000,output_tokens_max=1024"
        ],
        "profile": "concurrent",
        "rate": [32],
        "backend": "openai_http",
        "backend_kwargs": {
            "target": "http://blaze-superpod-server:8000",
            "model": "deepseek-ai/DeepSeek-R1-0528",
            "api_key": "test_api_key",
        },
        "processor": "deepseek-ai/DeepSeek-R1-0528",
        "max_seconds": 3600,
        "max_errors": 500,
    },
    "benchmarks": [
        {
            "type_": "generative_benchmark",
            "id_": "1b33b4d1-5b31-47a2-bc49-f479a6a66020",
            "run_id": "7313a0e4-3220-472b-8a8a-a83cdd07bc80",
            "run_index": 0,
            "config": {
                "strategy": {
                    "type_": "concurrent",
                    "worker_count": 10,
                    "max_concurrency": 32,
                    "streams": 32,
                },
                "backend": {
                    "target": "http://blaze-superpod-server:8000",
                    "model": "deepseek-ai/DeepSeek-R1-0528",
                    "api_key": "test_api_key",
                    "http2": True,
                },
                "profile": {"type_": "concurrent"},
            },
            "scheduler_state": {
                "node_id": 0,
                "num_processes": 10,
                "start_time": 1777451316.0,
                "end_time": 1777451697.0,
                "start_requests_time": 1777451316.4,
                "end_requests_time": 1777451697.25,
                "end_queuing_time": 1777451697.25,
                "end_processing_time": 1777451697.25,
                "created_requests": 706,
                "queued_requests": 0,
                "successful_requests": 32,
                "errored_requests": 500,
                "cancelled_requests": 174,
            },
            "scheduler_metrics": {
                "start_time": 1777451316.0,
                "request_start_time": 1777451316.4,
                "measure_start_time": 1777451496.4,
                "measure_end_time": 1777451697.25,
                "request_end_time": 1777451697.25,
                "end_time": 1777451697.0,
                "queued_time_avg": 94.5,
                "request_time_avg": 9.45,
                "resolve_time_avg": 21.6,
            },
            "metrics": {
                "request_totals": {
                    "successful": 32,
                    "errored": 500,
                    "incomplete": 31,
                    "total": 531,
                },
                "time_to_first_token_ms": {
                    "successful": {
                        "mean": 49.23,
                        "median": 48.7,
                        "std_dev": 5.58,
                        "percentiles": {"p50": 48.5, "p99": 77.79},
                    },
                    "errored": {
                        "mean": 199.15,
                        "median": 0.0,
                        "std_dev": 771.06,
                        "percentiles": {"p50": 0.0, "p99": 3476.5},
                    },
                    "incomplete": {"mean": 0.0, "percentiles": {"p99": 0.0}},
                    "total": {
                        "mean": 187.52,
                        "median": 0.0,
                        "percentiles": {"p99": 3476.5},
                    },
                },
                "time_per_output_token_ms": {
                    "successful": {
                        "mean": 11.99,
                        "median": 11.62,
                        "std_dev": 1.75,
                        "percentiles": {"p99": 23.59},
                    },
                },
                "inter_token_latency_ms": {
                    "successful": {
                        "mean": 6.79,
                        "median": 6.77,
                        "std_dev": 0.06,
                        "percentiles": {"p99": 7.11},
                    },
                },
                "requests_per_second": {
                    "successful": {"mean": 2.49, "median": 0.01},
                    "total": {"mean": 2.49, "median": 0.01},
                },
                "output_tokens_per_second": {
                    "successful": {"mean": 51.76},
                },
                "tokens_per_second": {
                    "successful": {"mean": 616.86},
                },
                "prompt_token_count": {
                    "successful": {"total_sum": 208831, "count": 500},
                },
                "output_token_count": {
                    "successful": {"total_sum": 19128, "count": 500},
                },
            },
        }
    ],
}


@pytest.fixture
def guidellm_pipeline():
    p = MagicMock()
    p.pipeline_start_ts = "2026-04-29T15:00:00Z"
    p.pipeline_end_ts = "2026-04-29T16:00:00Z"
    p.project = "tt-inference-server"
    p.git_commit_hash = "abc123"
    p.pipeline_submission_ts = "2026-04-29T14:00:00Z"
    p.git_branch_name = "main"
    p.github_pipeline_id = 88888
    p.github_pipeline_link = "http://example.com/run/88888"
    p.git_author = "marko"
    p.jobs = [
        MagicMock(
            github_job_id=1,
            job_start_ts="2026-04-29T15:10:00Z",
            job_end_ts="2026-04-29T15:50:00Z",
            docker_image="guidellm_image",
            host_name="bench_host",
        )
    ]
    return p


@pytest.fixture
def guidellm_mapper():
    return GuideLLMBenchmarkDataMapper()


def test_guidellm_produces_run_per_benchmark(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    assert len(result) == 1
    assert isinstance(result[0], CompleteBenchmarkRun)


def test_guidellm_produces_two_runs_for_two_benchmarks(guidellm_mapper, guidellm_pipeline):
    data = {
        **SAMPLE_GUIDELLM_OUTPUT,
        "benchmarks": SAMPLE_GUIDELLM_OUTPUT["benchmarks"] * 2,
    }
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, data)
    assert len(result) == 2


def test_guidellm_run_type(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    assert result[0].run_type == "guidellm_benchmark"


def test_guidellm_model_name_strips_prefix(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    assert result[0].ml_model_name == "DeepSeek-R1-0528"


def test_guidellm_batch_size_from_max_concurrency(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    assert result[0].batch_size == 32


def test_guidellm_input_output_seq_length_from_args_data(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    assert result[0].input_sequence_length == 400
    assert result[0].output_sequence_length == 3000


def test_guidellm_dataset_name_is_processor(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    assert result[0].dataset_name == "deepseek-ai/DeepSeek-R1-0528"


def test_guidellm_dataset_name_missing_processor(guidellm_mapper, guidellm_pipeline):
    data = {**SAMPLE_GUIDELLM_OUTPUT, "args": {**SAMPLE_GUIDELLM_OUTPUT["args"]}}
    data["args"].pop("processor", None)
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, data)
    assert result[0].dataset_name is None


def test_guidellm_flattens_all_numeric_metrics(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    by_name = {m.name: m.value for m in result[0].measurements}
    assert by_name["metrics_time_to_first_token_ms_successful_mean"] == pytest.approx(49.23)
    assert by_name["metrics_time_to_first_token_ms_successful_percentiles_p99"] == pytest.approx(77.79)
    assert by_name["metrics_time_per_output_token_ms_successful_mean"] == pytest.approx(11.99)
    assert by_name["metrics_inter_token_latency_ms_successful_std_dev"] == pytest.approx(0.06)
    assert by_name["metrics_requests_per_second_total_mean"] == pytest.approx(2.49)
    assert by_name["metrics_request_totals_total"] == 531
    assert by_name["metrics_prompt_token_count_successful_total_sum"] == 208831
    assert by_name["scheduler_state_successful_requests"] == 32
    assert by_name["scheduler_state_errored_requests"] == 500
    assert by_name["scheduler_metrics_request_time_avg"] == pytest.approx(9.45)
    assert by_name["duration"] == pytest.approx(381.0)


def test_guidellm_skips_non_numeric_leaves(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    names = {m.name for m in result[0].measurements}
    # Non-numeric leaves should not appear as measurements
    assert not any("type_" in n for n in names)
    assert not any("api_key" in n for n in names)


def test_guidellm_measurement_step_name(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    for m in result[0].measurements:
        assert m.step_name == "guidellm_benchmark"


def test_guidellm_config_params_contains_metadata_and_args(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    cfg = result[0].config_params
    assert cfg["metadata"]["guidellm_version"] == "0.6.0"
    assert cfg["args"]["profile"] == "concurrent"
    assert cfg["config"]["strategy"]["max_concurrency"] == 32
    assert cfg["benchmark_id"] == "1b33b4d1-5b31-47a2-bc49-f479a6a66020"
    assert cfg["data_spec"] == SAMPLE_GUIDELLM_OUTPUT["args"]["data"][0]


def test_guidellm_redacts_api_key(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    cfg = result[0].config_params
    assert cfg["args"]["backend_kwargs"]["api_key"] == "***REDACTED***"
    assert cfg["config"]["backend"]["api_key"] == "***REDACTED***"


def test_guidellm_pipeline_metadata_propagated(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, SAMPLE_GUIDELLM_OUTPUT)
    run = result[0]
    assert run.git_repo_name == "tt-inference-server"
    assert run.git_commit_hash == "abc123"
    assert run.git_branch_name == "main"
    assert run.github_pipeline_id == 88888
    assert run.user_name == "marko"
    assert run.device_hostname == "bench_host"


def test_guidellm_no_job_found(guidellm_mapper, guidellm_pipeline):
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 999, SAMPLE_GUIDELLM_OUTPUT)
    assert result is None


def test_guidellm_handles_missing_blocks(guidellm_mapper, guidellm_pipeline):
    minimal = {
        "metadata": {"guidellm_version": "0.6.0"},
        "args": {"data": ["prompt_tokens=100,output_tokens=200"]},
        "benchmarks": [
            {
                "config": {
                    "backend": {"model": "test/model"},
                    "strategy": {"max_concurrency": 4},
                },
            }
        ],
    }
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, minimal)
    assert len(result) == 1
    assert result[0].ml_model_name == "model"
    assert result[0].batch_size == 4
    assert result[0].input_sequence_length == 100
    assert result[0].output_sequence_length == 200
    assert result[0].measurements == []


def test_guidellm_skips_nan_inf(guidellm_mapper, guidellm_pipeline):
    data = {
        "args": {"data": ["prompt_tokens=10,output_tokens=20"]},
        "benchmarks": [
            {
                "config": {"backend": {"model": "x/y"}, "strategy": {"max_concurrency": 1}},
                "metrics": {
                    "request_latency": {
                        "successful": {
                            "mean": float("nan"),
                            "max": float("inf"),
                            "min": -float("inf"),
                            "median": 5.0,
                        }
                    }
                },
            }
        ],
    }
    result = guidellm_mapper.map_benchmark_data(guidellm_pipeline, 1, data)
    by_name = {m.name: m.value for m in result[0].measurements}
    assert "metrics_request_latency_successful_median" in by_name
    assert "metrics_request_latency_successful_mean" not in by_name
    assert "metrics_request_latency_successful_max" not in by_name
    assert "metrics_request_latency_successful_min" not in by_name


# --- acceptance_criteria tests ---


def _acceptance_run(result):
    runs = [r for r in result if r.run_type == "acceptance_criteria"]
    assert len(runs) == 1
    return runs[0]


# A realistic acceptance_criteria block as emitted by a Shield release report.
_AC_BLOCKERS = ["latency above threshold", "accuracy regression"]
_AC_METADATA = {"enforcement_result": "FAIL", "model_status": "FUNCTIONAL"}
_AC_SUMMARY_MD = "## Acceptance summary\n- latency: FAIL\n- accuracy: FAIL"


def test_process_acceptance_criteria_pass(mapper, pipeline):
    report_data = {
        "metadata": {"model_name": "Llama-3.2-1B-Instruct", "device": "t3k"},
        "acceptance_criteria": True,
    }
    run = _acceptance_run(mapper.map_benchmark_data(pipeline, 1, report_data))
    assert run.ml_model_name == "Llama-3.2-1B-Instruct"
    assert run.device_info == {"device_name": "t3k"}
    by_name = {m.name: m.value for m in run.measurements}
    assert by_name == {"passed": 1.0}


def test_process_acceptance_criteria_fail(mapper, pipeline):
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_criteria": False,
    }
    run = _acceptance_run(mapper.map_benchmark_data(pipeline, 1, report_data))
    by_name = {m.name: m.value for m in run.measurements}
    assert by_name == {"passed": 0.0}


def test_process_acceptance_criteria_non_bool_skips_run(mapper, pipeline):
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_criteria": "PASS",
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert [r for r in result if r.run_type == "acceptance_criteria"] == []


def test_process_acceptance_criteria_absent_skips_run(mapper, pipeline):
    report_data = {"metadata": {"model_name": "m", "device": "d"}, "benchmarks": []}
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert [r for r in result if r.run_type == "acceptance_criteria"] == []


def test_acceptance_config_fields_alone_do_not_emit_run(mapper, pipeline):
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_blockers": _AC_BLOCKERS,
        "acceptance_criteria_metadata": _AC_METADATA,
        "acceptance_summary_markdown": _AC_SUMMARY_MD,
    }
    result = mapper.map_benchmark_data(pipeline, 1, report_data)
    assert [r for r in result if r.run_type == "acceptance_criteria"] == []


def test_acceptance_config_params_includes_report_fields(mapper, pipeline):
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_criteria": False,
        "acceptance_blockers": _AC_BLOCKERS,
        "acceptance_criteria_metadata": _AC_METADATA,
        "acceptance_summary_markdown": _AC_SUMMARY_MD,
    }
    run = _acceptance_run(mapper.map_benchmark_data(pipeline, 1, report_data))
    assert run.config_params == {
        "acceptance_blockers": _AC_BLOCKERS,
        "acceptance_criteria_metadata": _AC_METADATA,
        "acceptance_summary_markdown": _AC_SUMMARY_MD,
    }


def test_acceptance_config_params_merges_model_spec_and_report_fields(mapper, pipeline):
    model_spec_data = {
        "model_id": "test_model",
        "model_type": "llm",
        "docker_image": "spec_image",
    }
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_criteria": True,
        "acceptance_blockers": _AC_BLOCKERS,
        "acceptance_criteria_metadata": _AC_METADATA,
        "acceptance_summary_markdown": _AC_SUMMARY_MD,
    }
    run = _acceptance_run(mapper.map_benchmark_data(pipeline, 1, report_data, model_spec_data))
    assert run.ml_model_type == "llm"
    assert run.config_params == {
        "model_id": "test_model",
        "model_type": "llm",
        "docker_image": "spec_image",
        "acceptance_blockers": _AC_BLOCKERS,
        "acceptance_criteria_metadata": _AC_METADATA,
        "acceptance_summary_markdown": _AC_SUMMARY_MD,
    }


def test_acceptance_config_params_none_when_nothing_to_record(mapper, pipeline):
    """With no model_spec_data and none of the acceptance_* fields, config_params is None."""
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_criteria": True,
    }
    run = _acceptance_run(mapper.map_benchmark_data(pipeline, 1, report_data))
    assert run.config_params is None


def test_acceptance_config_params_omits_absent_and_none_fields(mapper, pipeline):
    """Only present, non-None acceptance_* fields land in config_params."""
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_criteria": True,
        "acceptance_blockers": _AC_BLOCKERS,
        "acceptance_criteria_metadata": None,  # explicit None is dropped
        # acceptance_summary_markdown absent entirely
    }
    run = _acceptance_run(mapper.map_benchmark_data(pipeline, 1, report_data))
    assert run.config_params == {"acceptance_blockers": _AC_BLOCKERS}


def test_acceptance_config_params_preserves_empty_collections(mapper, pipeline):
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_criteria": True,
        "acceptance_blockers": [],
    }
    run = _acceptance_run(mapper.map_benchmark_data(pipeline, 1, report_data))
    assert run.config_params == {"acceptance_blockers": []}


def test_acceptance_config_params_does_not_mutate_model_spec(mapper, pipeline):
    model_spec_data = {"model_id": "test_model"}
    report_data = {
        "metadata": {"model_name": "m", "device": "d"},
        "acceptance_criteria": True,
        "acceptance_blockers": _AC_BLOCKERS,
    }
    mapper.map_benchmark_data(pipeline, 1, report_data, model_spec_data)
    assert model_spec_data == {"model_id": "test_model"}
