# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import pytest
import json
import tempfile
from pathlib import Path
from parsers.vllm_bench_serve_parser import VllmBenchServeParser


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

SAMPLE_RAW_VLLM_OUTPUT = {
    **SAMPLE_VLLM_BENCH_OUTPUT,
    "endpoint_type": "openai-chat",
    "backend": "openai-chat",
    "label": None,
    "request_rate": "inf",
    "burstiness": 1,
    "request_goodput": None,
    "max_concurrent_requests": 1000,
}


def _write_json_tempfile(data):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


@pytest.fixture
def sample_bench_file():
    path = _write_json_tempfile(SAMPLE_VLLM_BENCH_OUTPUT)
    yield path
    Path(path).unlink()


@pytest.fixture
def sample_raw_bench_file():
    path = _write_json_tempfile(SAMPLE_RAW_VLLM_OUTPUT)
    yield path
    Path(path).unlink()


class TestCanParse:
    def test_accepts_vllm_bench_json(self, sample_bench_file):
        parser = VllmBenchServeParser()
        assert parser.can_parse(sample_bench_file) is True

    def test_accepts_raw_vllm_output(self, sample_raw_bench_file):
        parser = VllmBenchServeParser()
        assert parser.can_parse(sample_raw_bench_file) is True

    def test_rejects_non_json(self):
        parser = VllmBenchServeParser()
        assert parser.can_parse("report.xml") is False

    def test_rejects_unrelated_json(self):
        path = _write_json_tempfile({"parameter_support_tests": {"results": {}}})
        try:
            parser = VllmBenchServeParser()
            assert parser.can_parse(path) is False
        finally:
            Path(path).unlink()


class TestParse:
    def test_produces_single_test(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        assert len(tests) == 1

    def test_test_case_name_is_model_id(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        assert tests[0].test_case_name == "deepseek-ai/DeepSeek-R1-0528"

    def test_category_and_owner(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        assert tests[0].category == "vllm_benchmark"
        assert tests[0].owner == "tt-inference-server"

    def test_tags(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        assert tests[0].tags["type"] == "vllm_bench_serve"

    def test_full_test_name(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        expected = "vllm_bench_serve::deepseek-ai/DeepSeek-R1-0528::concurrency_64::prompts_1000"
        assert tests[0].full_test_name == expected

    def test_success_when_no_failures(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        assert tests[0].success is True
        assert tests[0].error_message is None

    def test_failure_when_requests_fail(self):
        data = {**SAMPLE_VLLM_BENCH_OUTPUT, "failed": 5, "completed": 995}
        path = _write_json_tempfile(data)
        try:
            parser = VllmBenchServeParser()
            tests = parser.parse(path)
            assert tests[0].success is False
            assert "5 requests failed" in tests[0].error_message
        finally:
            Path(path).unlink()

    def test_timestamps_parsed_from_date_and_duration(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        test = tests[0]
        assert test.test_start_ts.year == 2026
        assert test.test_start_ts.month == 3
        assert test.test_start_ts.day == 18
        assert test.test_start_ts.hour == 15
        assert test.test_start_ts.minute == 42
        assert test.test_end_ts > test.test_start_ts

    def test_config_contains_parameters(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        config = tests[0].config
        assert config["model_id"] == "deepseek-ai/DeepSeek-R1-0528"
        assert config["tokenizer_id"] == "deepseek-ai/DeepSeek-R1-0528"
        assert config["num_prompts"] == 1000
        assert config["max_concurrency"] == 64
        assert config["completed"] == 1000
        assert config["failed"] == 0

    def test_config_contains_metrics(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        metrics = tests[0].config["metrics"]
        assert metrics["mean_ttft_ms"] == pytest.approx(49.234016701811925)
        assert metrics["mean_tpot_ms"] == pytest.approx(0.010592051617808856)
        assert metrics["mean_itl_ms"] == pytest.approx(0.010470757914358829)
        assert metrics["request_throughput"] == pytest.approx(1131.668563408123)
        assert metrics["output_throughput"] == pytest.approx(144687.22083741875)
        assert metrics["total_token_throughput"] == pytest.approx(288409.1283902503)
        assert metrics["p99_ttft_ms"] == pytest.approx(77.79025232302956)

    def test_raw_vllm_output_also_parses(self, sample_raw_bench_file):
        """Ensure the parser handles the full raw vLLM output (with extra fields)."""
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_raw_bench_file)
        assert len(tests) == 1
        assert tests[0].test_case_name == "deepseek-ai/DeepSeek-R1-0528"

    def test_group_is_model_id(self, sample_bench_file):
        parser = VllmBenchServeParser()
        tests = parser.parse(sample_bench_file)
        assert tests[0].group == "deepseek-ai/DeepSeek-R1-0528"

    def test_empty_file_returns_empty(self):
        path = _write_json_tempfile({})
        try:
            parser = VllmBenchServeParser()
            tests = parser.parse(path)
            assert tests == []
        finally:
            Path(path).unlink()
