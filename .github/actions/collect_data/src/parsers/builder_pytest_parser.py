# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from loguru import logger
from functools import partial
from pydantic_models import OpTest, TensorDesc, TestStatus, Backend
from datetime import datetime, timedelta
from typing import Optional
from .parser import Parser
from . import junit_xml_utils
from utils import parse_timestamp
import ast
from pydantic import ValidationError
from shared import failure_happened, is_valid_testcase_

# Mapping from backend string to backend enum
BACKEND_STR_TO_ENUM: dict[str, Backend] = {
    "ttnn": Backend.ttnn,
    "ttmetal": Backend.ttmetal,
    "ttnn-standalone": Backend.ttnn,
}

FAILURE_STAGE_TO_STATUS_ENUM: dict[str, TestStatus] = {
    "compile": TestStatus.compile_failed,
    "golden": TestStatus.golden_failed,
    "runtime": TestStatus.run_failed,
    "success": TestStatus.success,
}


class BuilderPytestParser(Parser):
    """Parser for builder pytest report files."""

    def can_parse(self, filepath: str) -> bool:
        if not filepath.endswith(".xml"):
            return False
        try:
            report_root_tree = junit_xml_utils.get_xml_file_root_element_tree(filepath)
            report_root = report_root_tree.getroot()
            is_pytest = junit_xml_utils.is_pytest_junit_xml(report_root)

            # Additional check for builder specific tests
            if is_pytest:
                testsuite = report_root[0]
                # Look for card type property which indicates builder tests
                properties = testsuite.find("properties")
                if properties is not None:
                    for prop in properties.findall("property"):
                        if prop.get("name") == "card":
                            return True
            return False
        except Exception:
            return False

    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ) -> list:
        return get_tests(filepath, project, github_job_id)


def get_tests(filepath, project: Optional[str] = None, github_job_id: Optional[int] = None):
    report_root_tree = junit_xml_utils.get_xml_file_root_element_tree(filepath)
    report_root = report_root_tree.getroot()
    testsuite = report_root[0]
    default_timestamp = parse_timestamp(testsuite.attrib["timestamp"])

    # Extract card type and git SHA from testsuite properties
    card_type = _get_card_type(testsuite)
    git_sha = _get_git_sha(testsuite)

    get_pydantic_test = partial(
        get_pydantic_optest_from_pytest_testcase_,
        default_timestamp=default_timestamp,
        project=project,
        github_job_id=github_job_id,
        card_type=card_type,
        git_sha=git_sha,
    )
    tests = []
    for testcase in testsuite:
        if is_valid_testcase_(testcase):
            test = get_pydantic_test(testcase)
            if test:
                tests.append(test)
    return tests


def _get_card_type(testsuite) -> str:
    """Extract card type from testsuite properties."""
    properties = testsuite.find("properties")
    try:
        for prop in properties.findall("property"):
            if prop.get("name") == "card":
                return prop.get("value")
        raise KeyError
    except Exception:
        raise KeyError("Unable to find 'card' property in suite")


def _get_git_sha(testsuite) -> str:
    """Extract git SHA from testsuite properties."""
    properties = testsuite.find("properties")
    try:
        for prop in properties.findall("property"):
            if prop.get("name") == "git_sha":
                return prop.get("value")
        raise KeyError
    except Exception:
        raise KeyError("Unable to find 'git_sha' property in suite")


def get_pydantic_optest_from_pytest_testcase_(
    testcase,
    card_type: str,
    git_sha: str,
    github_job_id: Optional[int] = None,
    default_timestamp: Optional[datetime] = datetime.now(),
    project: Optional[str] = None,
):
    skipped = junit_xml_utils.get_pytest_testcase_is_skipped(testcase)
    failed = junit_xml_utils.get_pytest_testcase_is_failed(testcase)
    error = junit_xml_utils.get_pytest_testcase_is_error(testcase)
    success = not (failed or error)

    error_message = None

    # First try to get error message from XML properties if it exists
    properties = {}
    try:
        properties = junit_xml_utils.get_pytest_testcase_properties(testcase)
        error_message = properties.get("error_message")
    except:
        pass

    # Fallback to junit XML error/failure messages if no error_message property
    if error_message is None:
        # Error is scarier than failure, expose that first
        if failed:
            error_message = junit_xml_utils.get_pytest_failure_message(testcase)

        if error:
            error_message = junit_xml_utils.get_pytest_error_message(testcase)

        if skipped:
            error_message = junit_xml_utils.get_pytest_skipped_message(testcase)

    test_duration = float(testcase.attrib["time"])

    # Error at the beginning of a test can prevent pytest from recording timestamps at all
    if not (skipped or error) and "start_timestamp" in properties:
        test_start_ts = parse_timestamp(properties["start_timestamp"])
        test_end_ts = test_start_ts + timedelta(seconds=test_duration)
    else:
        test_start_ts = default_timestamp
        test_end_ts = default_timestamp + timedelta(seconds=test_duration)

    test_name = testcase.attrib["name"]
    test_case_name = test_name.split("[")[0]

    filepath_no_ext = testcase.attrib["classname"].replace(".", "/")
    filepath = f"{filepath_no_ext}.py"

    full_test_name = f"{filepath}::{test_name}"

    # Extract test parameters from prefixed properties (param_*)
    config = {}
    for key, value in properties.items():
        if key.startswith("param_"):
            param_name = key[6:]  # Remove "param_" prefix
            config[param_name] = value

    # Determine backend from XML properties
    backend_str = properties.get("backend")
    if backend_str is None:
        raise ValueError("Missing 'backend' property in XML")

    # Determine test status from failure_stage property
    failure_stage = properties.get("failure_stage")

    if failure_stage is not None:
        try:
            status = FAILURE_STAGE_TO_STATUS_ENUM[failure_stage]
        except KeyError:
            raise ValueError("Invalid status string: {failure_stage}")
    else:  # TODO: think about how to handle tests with null failure_stage
        # No failure_stage property means test was skipped before execution
        if skipped:
            status = None  # Skipped tests don't have a meaningful status
        elif failed or error:
            status = TestStatus.run_failed  # Fallback for failed tests without failure_stage
        else:
            status = TestStatus.success  # Fallback for successful tests without failure_stage

    # Extract operation information from XML properties or fallback to test name
    op_name = properties.get("op_name", test_case_name)
    framework_op_name = properties.get("framework_op_name", test_case_name)

    op_kind = "builder_op"  # Default for builder tests

    # Parse tensor information from XML properties (preferred) or test parameters
    inputs = []

    # For now, assume no outputs until golden checking is implemented into pytest.
    outputs = []

    # First try to extract from XML properties we added
    input_shapes_str = properties.get("input_shapes")
    if input_shapes_str is None:
        raise ValueError("Missing 'input_shapes' property in XML")

    input_dtypes_str = properties.get("input_dtypes")
    if input_dtypes_str is None:
        raise ValueError("Missing 'input_dtypes' property in XML")

    try:
        # Parse shapes and dtypes from XML properties
        shapes_list = ast.literal_eval(input_shapes_str)
        dtypes_list = ast.literal_eval(input_dtypes_str)

        for (shape, dtype) in zip(shapes_list, dtypes_list):
            if not isinstance(shape, (list, tuple)):
                shape = [shape]  # Handle single dimension

            tensor_desc = TensorDesc(
                shape=list(shape),
                data_type=dtype,
                buffer_type="DRAM",  # default
                layout="ROW_MAJOR",  # default
                grid_shape=[1, 1],  # default
            )
            inputs.append(tensor_desc)
    except (ValueError, SyntaxError, TypeError) as e:
        logger.error(f"Error parsing tensor info from XML properties: {e}")
        pass

    try:
        return OpTest(
            github_job_id=github_job_id or 0,
            full_test_name=full_test_name,
            test_start_ts=test_start_ts,
            test_end_ts=test_end_ts,
            test_case_name=test_case_name,
            filepath=filepath,
            success=success,
            skipped=skipped,
            error_message=error_message,
            config=config,
            frontend=project or "builder",
            model_name="builder_ops",  # Default model name for builder tests
            op_kind=op_kind,
            op_name=op_name,
            framework_op_name=framework_op_name,
            inputs=inputs,
            outputs=outputs,
            op_params=config,
            git_sha=git_sha,
            status=status,
            card_type=card_type,
            backend=backend_str,
        )
    except ValidationError as e:
        failure_happened()
        logger.error(f"Validation error: {e}")
        return None
