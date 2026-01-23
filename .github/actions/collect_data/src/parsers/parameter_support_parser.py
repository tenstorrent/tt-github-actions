# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from loguru import logger
from pydantic_models import Test
from datetime import datetime, timedelta
from typing import Optional, List
from .parser import Parser
from pydantic import ValidationError
from shared import failure_happened
import json


class ParameterSupportParser(Parser):
    """Parser for parameter support test JSON report files."""

    def can_parse(self, filepath: str):
        """Check if file is JSON and contains parameter support test results."""
        if not filepath.endswith(".json"):
            return False

        try:
            with open(filepath, "r") as f:
                data = json.load(f)
                # Check if it has the parameter_support_tests structure
                return "parameter_support_tests" in data and "results" in data.get("parameter_support_tests", {})
        except (json.JSONDecodeError, IOError, KeyError) as e:
            logger.error(f"Failed to load JSON from {filepath}: {e}")
            return False

    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ) -> List[Test]:
        logger.info(f"Parsing parameter support tests from {filepath}")

        try:
            with open(filepath, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load JSON from {filepath}: {e}")
            return []

        param_tests = data.get("parameter_support_tests", {})
        if not param_tests or "results" not in param_tests:
            logger.warning(f"No parameter support test results found in {filepath}")
            return []

        tests = []
        model_name = param_tests.get("model_name", "unknown_model")
        model_impl = param_tests.get("model_impl", "unknown_impl")
        endpoint_url = param_tests.get("endpoint_url", "")

        for test_case_name, test_case_results in param_tests.get("results", {}).items():
            for test_case in test_case_results:
                base_timestamp = datetime.now()  # Placeholder timestamp
                test = _create_test_from_case(
                    test_case=test_case,
                    test_case_name=test_case_name,
                    model_name=model_name,
                    model_impl=model_impl,
                    endpoint_url=endpoint_url,
                    base_timestamp=base_timestamp,
                )
                if test:
                    tests.append(test)

        logger.info(f"Parsed {len(tests)} parameter support tests from {filepath}")
        return tests


def _create_test_from_case(
    test_case: dict,
    test_case_name: str,
    model_name: str,
    model_impl: str,
    endpoint_url: str,
    base_timestamp: datetime,
) -> Optional[Test]:
    """Convert a parameter support test case to a Test object."""

    test_duration = 1.0  # Placeholder duration in seconds
    test_start_ts = base_timestamp
    test_end_ts = base_timestamp + timedelta(seconds=test_duration)

    filepath = "test_vllm_server_parameters.py"  # Placeholder filepath

    status = test_case.get("status", "unknown").lower()
    message = test_case.get("message", "")

    success = status.lower() in ["passed", "success", "pass", "ok"]
    failed = status.lower() in ["failed", "failure", "fail", "error"]
    skipped = status.lower() in ["skipped", "skip"]

    error_message = None
    if failed or skipped:
        error_message = message

    full_test_name = f"{filepath}::{test_case_name}"

    config = {
        "model_name": model_name,
        "model_impl": model_impl,
        "endpoint_url": endpoint_url,
    }

    tags = {
        "type": "parameter_support_test",
    }

    try:
        return Test(
            test_start_ts=test_start_ts,
            test_end_ts=test_end_ts,
            test_case_name=test_case_name,
            filepath=filepath,
            category="parameter_support",
            group=test_case_name,
            owner="tt-shield",
            error_message=error_message,
            success=success,
            skipped=skipped,
            full_test_name=full_test_name,
            config=config,
            tags=tags,
        )
    except ValidationError as e:
        failure_happened()
        logger.error(f"Validation error creating Test for {test_case_name}: {e}")
        return None
