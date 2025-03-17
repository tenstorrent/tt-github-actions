# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import tarfile
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


class OpCompilationStatus(IntEnum):
    NOT_STARTED = 0
    CREATED_GRAPH = 1
    CONVERTED_TO_TORCH_IR = 2
    CONVERTED_TO_TORCH_BACKEND_IR = 3
    CONVERTED_TO_STABLE_HLO = 4
    CONVERTED_TO_TTIR = 5
    CONVERTED_TO_TTNN = 6
    EXECUTED = 7


class TTTorchModelTestsParser(Parser):
    """Parser for python unitest report files."""

    def can_parse(self, filepath: str):
        basename = os.path.basename(filepath)
        return basename.startswith("run") and basename.endswith(".tar")

    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ):
        return _get_tests(filepath, project, github_job_id)


def _untar(filepath):
    basename = os.path.basename(filepath)
    path = f"/tmp/{basename}"
    with tarfile.open(filepath, "r") as fd:
        fd.extractall(path=path)
    return path


def _all_json_files(filepath):
    for root, dirs, files in os.walk(filepath):
        for file in files:
            if file.endswith(".json") and not file.startswith("."):
                yield os.path.join(root, file)


def _get_tests_from_json(project, github_job_id, filepath):
    with open(filepath, "r") as fd:
        data = json.load(fd)

    for name, test in data.items():
        yield _get_pydantic_test(filepath, name, test, project, github_job_id)


def _get_pydantic_test(filepath, name, test, project, github_job_id, default_timestamp=datetime.now()):
    status = OpCompilationStatus(test["compilation_status"])

    skipped = False
    failed = status < OpCompilationStatus.EXECUTED
    error = False
    success = not (failed or error)
    error_message = str(status).split(".")[1]

    properties = {}

    test_start_ts = default_timestamp
    test_end_ts = default_timestamp

    model_name = os.path.basename(filepath).split(".")[0]

    # leaving empty for now
    group = None
    owner = None

    full_test_name = f"{filepath}::{name}"

    # to be populated with [] if available
    config = None

    tags = None

    try:
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
            model_name=test.get("model_name", model_name),
            op_kind="",
            op_name=test.get("framework_op_name", ""),
            framework_op_name=test.get("framework_op_name", ""),
            inputs=_map_tensor_desc(test.get("input_tensors")),
            outputs=_map_tensor_desc(test.get("output_tensors")),
            op_params=None,
        )
    except ValidationError as e:
        global report_failure
        report_failure = True
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


def _flatten(list_of_lists):
    return [item for sublist in list_of_lists for item in sublist]


def _get_tests(filepath, project, github_job_id):
    untar_path = _untar(filepath)
    tests = map(partial(_get_tests_from_json, project, github_job_id), _all_json_files(untar_path))
    return _flatten(tests)
