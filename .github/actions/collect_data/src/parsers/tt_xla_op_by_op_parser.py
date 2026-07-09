# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import os
import json
from functools import partial
from loguru import logger
from datetime import datetime
from pydantic_models import OpTest, TensorDesc
from .parser import Parser
from enum import IntEnum
from typing import Optional
from pydantic import ValidationError
from shared import failure_happened


class TTXlaOpByOpParser(Parser):
    """Parser for python unitest report files."""

    def can_parse(self, filepath: str):
        # Remove trailing slash if present
        filepath = filepath.rstrip("/")
        basename = os.path.basename(filepath)
        return "op-by-op" in basename or "op_by_op" in basename

    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ):
        return _get_tests(filepath, project, github_job_id)


def _all_json_files(filepath):
    for root, dirs, files in os.walk(filepath):
        for file in files:
            if file.endswith(".json") and not file.startswith("."):
                yield os.path.join(root, file)


def _get_tests_from_json(project, github_job_id, filepath):
    with open(filepath, "r") as fd:
        data = json.load(fd)

    # Extract OpTest entries from tests/user_properties
    if "tests" not in data or len(data["tests"]) == 0:
        return

    user_properties = data["tests"][0].get("user_properties", [])

    for prop in user_properties:
        # Look for entries with "OpTest model for: *" as the key
        for key, test_data in prop.items():
            if key.startswith("OpTest model for:"):
                # Extract op name from the key
                op_name = key.replace("OpTest model for:", "").strip()
                yield _get_pydantic_test(filepath, op_name, test_data, project, github_job_id)


def _get_pydantic_test(filepath, name, test, project, github_job_id, default_timestamp=datetime.now()):
    # Parse timestamps from test data
    test_start_ts = (
        datetime.fromisoformat(test["test_start_ts"])
        if test.get("test_start_ts") and test["test_start_ts"] != "None"
        else default_timestamp
    )
    test_end_ts = (
        datetime.fromisoformat(test["test_end_ts"])
        if test.get("test_end_ts") and test["test_end_ts"] != "None"
        else default_timestamp
    )

    # Parse success/skipped/error values
    success = test.get("success", "False") == "True"
    skipped = test.get("skipped", "False") == "True"
    error_message = test.get("error_message") if test.get("error_message") != "None" else None

    model_name = test.get("model_name", os.path.basename(filepath).split(".")[0])

    full_test_name = f"{filepath}::{name}"
    config = None

    try:
        # Parse inputs/outputs from string representation
        inputs = _parse_tensor_desc_list(test.get("inputs", "[]"))
        outputs = _parse_tensor_desc_list(test.get("outputs", "[]"))

        return OpTest(
            github_job_id=github_job_id,
            full_test_name=full_test_name,
            test_start_ts=test_start_ts,
            test_end_ts=test_end_ts,
            test_case_name=name,
            filepath=filepath,
            success=success,
            skipped=skipped,
            error_message=error_message,
            config=config,
            frontend=project,
            model_name=model_name,
            op_kind=test.get("op_kind", "") if test.get("op_kind") != "None" else "",
            op_name=test.get("op_name", "").strip('"') if test.get("op_name") != "None" else "",
            framework_op_name=test.get("framework_op_name", "") if test.get("framework_op_name") != "None" else "",
            inputs=inputs,
            outputs=outputs,
            op_params=None,
        )
    except ValidationError as e:
        failure_happened()
        logger.error(f"Validation error: {e}")
        return None


def _map_tensor_desc(tensors):
    if not tensors:
        return []
    for tensor in tensors:
        yield TensorDesc(
            shape=tensor.get("shape"),
            data_type=tensor.get("data_type"),
            buffer_type=tensor.get("buffer_type"),
            layout=tensor.get("layout"),
            grid_shape=tensor.get("grid_shape"),
        )


def _parse_tensor_desc_list(tensor_str):
    """Parse tensor description from string representation like:
    '[TensorDesc(shape=[1, 3, 800, 1066], data_type='bf16', buffer_type=None, layout=None, grid_shape=None)]'
    """
    if not tensor_str or tensor_str == "[]":
        return []

    import re
    import ast

    result = []
    # Find all TensorDesc(...) patterns
    pattern = r"TensorDesc\(([^)]+)\)"
    matches = re.finditer(pattern, tensor_str)

    for match in matches:
        params_str = match.group(1)
        tensor_dict = {}

        # Parse each parameter
        # Match: key=value pairs (handling None, strings, and lists)
        param_pattern = r'(\w+)=(\[.*?\]|\'[^\']*\'|"[^"]*"|None|\w+)'
        for param_match in re.finditer(param_pattern, params_str):
            key = param_match.group(1)
            value_str = param_match.group(2)

            # Parse the value
            if value_str == "None":
                value = None
            elif value_str.startswith("["):
                # Parse list using ast.literal_eval
                try:
                    value = ast.literal_eval(value_str)
                except:
                    value = None
            elif value_str.startswith("'") or value_str.startswith('"'):
                value = value_str.strip("'\"")
            else:
                value = value_str

            tensor_dict[key] = value

        result.append(
            TensorDesc(
                shape=tensor_dict.get("shape") or [],
                data_type=tensor_dict.get("data_type") or "unknown",
                buffer_type=tensor_dict.get("buffer_type") or "unknown",
                layout=tensor_dict.get("layout") or "unknown",
                grid_shape=tensor_dict.get("grid_shape") or [],
            )
        )

    return result


def _flatten(list_of_lists):
    return [item for sublist in list_of_lists for item in sublist]


def _get_tests(filepath, project, github_job_id):
    tests = map(partial(_get_tests_from_json, project, github_job_id), _all_json_files(filepath))
    return _flatten(tests)
