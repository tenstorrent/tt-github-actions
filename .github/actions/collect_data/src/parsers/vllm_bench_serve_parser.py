# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, asdict
from loguru import logger
from pydantic_models import Test
from typing import Optional, List, ClassVar
from .parser import Parser
from pydantic import ValidationError
from shared import failure_happened
from datetime import datetime, timedelta, timezone
import json


CATEGORY = "vllm_benchmark"
OWNER = "tt-inference-server"

METRICS_KEYS = [
    "request_throughput",
    "output_throughput",
    "total_token_throughput",
    "max_output_tokens_per_s",
    "total_input_tokens",
    "total_output_tokens",
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
]

REQUIRED_FIELDS = {"model_id", "mean_ttft_ms", "mean_tpot_ms"}


@dataclass(frozen=True)
class VllmBenchServeConfig:
    _PROPS: ClassVar[tuple] = ("model_id", "tokenizer_id", "num_prompts", "max_concurrency")
    model_id: str = "unknown"
    tokenizer_id: str = "unknown"
    num_prompts: int = 0
    max_concurrency: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "VllmBenchServeConfig":
        return cls(**{k: data[k] for k in cls._PROPS if k in data})


@dataclass(frozen=True)
class VllmBenchServeTags:
    type: str = "vllm_bench_serve"


class VllmBenchServeParser(Parser):
    """Parser for vLLM bench serve JSON benchmark result files."""

    def can_parse(self, filepath: str) -> bool:
        if not filepath.endswith(".json"):
            return False

        try:
            with open(filepath, "r") as f:
                data = json.load(f)
                return REQUIRED_FIELDS.issubset(data.keys())
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load JSON from {filepath}: {e}")
            return False

    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ) -> List[Test]:
        logger.info(f"Parsing vLLM bench serve results from {filepath}")

        try:
            with open(filepath, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load JSON from {filepath}: {e}")
            return []

        if not REQUIRED_FIELDS.issubset(data.keys()):
            logger.warning(f"Missing required fields in {filepath}")
            return []

        test = self._create_test(data)
        if test is None:
            return []
        return [test]

    def _parse_date(self, date_str: str) -> datetime:
        """Parse vLLM bench date format '20260318-154232' into a timezone-aware datetime."""
        try:
            return datetime.strptime(date_str, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.warning(f"Could not parse date '{date_str}', using epoch")
            return datetime(1970, 1, 1, tzinfo=timezone.utc)

    def _create_test(self, data: dict) -> Optional[Test]:
        model_id = data.get("model_id", "unknown")
        max_concurrency = data.get("max_concurrency", 0)
        num_prompts = data.get("num_prompts", 0)

        test_start_ts = self._parse_date(data.get("date", ""))
        duration = data.get("duration", 0)
        test_end_ts = test_start_ts + timedelta(seconds=duration)

        completed = data.get("completed", 0)
        failed = data.get("failed", 0)
        success = completed > 0 and failed == 0

        error_message = None
        if failed > 0:
            error_message = f"{failed} requests failed out of {completed + failed}"

        config = asdict(VllmBenchServeConfig.from_dict(data))
        metrics = {k: data[k] for k in METRICS_KEYS if k in data}
        config["metrics"] = metrics
        config["completed"] = completed
        config["failed"] = failed
        config["duration"] = duration

        full_test_name = f"vllm_bench_serve::{model_id}::concurrency_{max_concurrency}::prompts_{num_prompts}"

        try:
            return Test(
                test_start_ts=test_start_ts,
                test_end_ts=test_end_ts,
                test_case_name=model_id,
                filepath="vllm_bench_serve",
                category=CATEGORY,
                group=model_id,
                owner=OWNER,
                error_message=error_message,
                success=success,
                skipped=False,
                full_test_name=full_test_name,
                config=config,
                tags=asdict(VllmBenchServeTags()),
            )
        except ValidationError as e:
            failure_happened()
            logger.error(f"Validation error creating Test for {model_id}: {e}")
            return None
