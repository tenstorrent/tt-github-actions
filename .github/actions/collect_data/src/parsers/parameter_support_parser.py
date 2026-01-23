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

        param_support_tests = data.get("parameter_support_tests", {})
        if not param_support_tests or "results" not in param_support_tests:
            logger.warning(f"No parameter support test results found in {filepath}")
            return []

        tests = []
        for test_group_name, test_case_results in param_support_tests.get("results", {}).items():
            for test_case in test_case_results:
                test = self._create_test_from_case(
                    param_support_tests=param_support_tests,
                    test_group_name=test_group_name,
                    test_case=test_case,
                )
                if test:
                    tests.append(test)

        logger.info(f"Parsed {len(tests)} parameter support tests from {filepath}")
        return tests

    def _create_test_from_case(
        self,
        param_support_tests: dict,
        test_group_name: str,
        test_case: dict,
    ) -> Optional[Test]:
        """Convert a parameter support test case to a Test object."""

        test_start_ts = test_case.get("test_start_ts", "9999-12-31T23:59:59Z")
        test_end_ts = test_case.get("test_end_ts", "9999-12-31T23:59:59Z")
        test_case_name = test_case.get("test_node_name", "unknown")

        if "test_id" in test_case:
            filepath = test_case["test_id"].split("::")[0]
        else:
            filepath = "unknown"

        status = test_case.get("status", "unknown").lower()
        message = test_case.get("message", "")
        success = status.lower() in ["passed", "success", "pass", "ok"]
        failed = status.lower() in ["failed", "failure", "fail", "error"]
        skipped = status.lower() in ["skipped", "skip"]

        error_message = None
        if failed or skipped:
            error_message = message

        full_test_name = test_case.get("test_id", "unknown")
        config = {
            "model_name": param_support_tests.get("model_name", "unknown_model"),
            "model_impl": param_support_tests.get("model_impl", "unknown_impl"),
            "endpoint_url": param_support_tests.get("endpoint_url", ""),
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
                group=test_group_name,
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
