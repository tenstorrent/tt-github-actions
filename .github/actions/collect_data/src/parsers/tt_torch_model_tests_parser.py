# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import tarfile
import os
import json
from loguru import logger
from functools import partial
from datetime import datetime
from pydantic_models import Test
from .parser import Parser
from . import junit_xml_utils
from enum import Enum, IntEnum


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

    def parse(self, filepath: str):
        return get_tests(filepath)


def untar(filepath):
    basename = os.path.basename(filepath)
    path = f"/tmp/{basename}"
    with tarfile.open(filepath, "r") as fd:
        fd.extractall(path=path)
    return path


def all_json_files(filepath):
    for root, dirs, files in os.walk(filepath):
        for file in files:
            if file.endswith(".json"):
                yield os.path.join(root, file)


def get_tests_from_json(filepath):
    with open(filepath, "r") as fd:
        data = json.load(fd)

    for name, test in data.items():
        yield get_pydantic_test(filepath, name, test)


def get_pydantic_test(filepath, name, test, default_timestamp=datetime.now()):
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

    return Test(
        test_start_ts=test_start_ts,
        test_end_ts=test_end_ts,
        test_case_name=name,
        filepath=filepath,
        category="models",
        group=None,
        owner=None,
        frontend="tt-torch",
        model_name=model_name,
        op_name=None,
        framework_op_name=test["torch_name"],
        op_kind=None,
        error_message=error_message,
        success=success,
        skipped=skipped,
        full_test_name=full_test_name,
        config=config,
        tags=tags,
    )


def flatten(list_of_lists):
    return [item for sublist in list_of_lists for item in sublist]


def get_tests(filepath):
    untar_path = untar(filepath)
    tests = map(get_tests_from_json, all_json_files(untar_path))
    return flatten(tests)
