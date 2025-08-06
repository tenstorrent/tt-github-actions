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
import html
from pydantic import ValidationError
from shared import failure_happened


class BuilderPytestParser(Parser):
    """Parser for builder pytest report files."""

    def can_parse(self, filepath: str):
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
                properties = testsuite.find('properties')
                if properties is not None:
                    for prop in properties.findall('property'):
                        if prop.get('name') == 'card':
                            return True
            return False
        except Exception:
            return False

    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ):
        return get_tests(filepath, project, github_job_id)


def get_tests(filepath, project=None, github_job_id=None):
    report_root_tree = junit_xml_utils.get_xml_file_root_element_tree(filepath)
    report_root = report_root_tree.getroot()
    testsuite = report_root[0]
    default_timestamp = parse_timestamp(testsuite.attrib["timestamp"])
    
    # Extract card type from testsuite properties
    card_type = _get_card_type(testsuite)
    
    get_pydantic_test = partial(
        get_pydantic_optest_from_pytest_testcase_, 
        default_timestamp=default_timestamp,
        project=project,
        github_job_id=github_job_id,
        card_type=card_type
    )
    tests = []
    for testcase in testsuite:
        if is_valid_testcase_(testcase):
            test = get_pydantic_test(testcase)
            if test:
                tests.append(test)
    return tests


def _get_card_type(testsuite):
    """Extract card type from testsuite properties."""
    properties = testsuite.find('properties')
    if properties is not None:
        for prop in properties.findall('property'):
            if prop.get('name') == 'card':
                return prop.get('value')
    return None


def get_pydantic_optest_from_pytest_testcase_(
    testcase, 
    default_timestamp=datetime.now(),
    project=None,
    github_job_id=None,
    card_type=None
):
    skipped = junit_xml_utils.get_pytest_testcase_is_skipped(testcase)
    failed = junit_xml_utils.get_pytest_testcase_is_failed(testcase)
    error = junit_xml_utils.get_pytest_testcase_is_error(testcase)
    success = not (failed or error)

    error_message = None

    # Error is scarier than failure, expose that first
    if failed:
        error_message = junit_xml_utils.get_pytest_failure_message(testcase)

    if error:
        error_message = junit_xml_utils.get_pytest_error_message(testcase)

    if skipped:
        error_message = junit_xml_utils.get_pytest_skipped_message(testcase)

    properties = {}
    try:
        properties = junit_xml_utils.get_pytest_testcase_properties(testcase)
    except:
        pass

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

    # Extract test parameters as config if present
    config = None
    try:
        config_string = properties.get("config")
        if config_string is not None:
            config = ast.literal_eval(html.unescape(config_string))
        elif '[' in test_name and ']' in test_name:
            # Extract parameters from test name for builder tests
            param_part = test_name[test_name.find('[') + 1:test_name.rfind(']')]
            if param_part:
                config = {'parameters': param_part.split('-')}
    except (ValueError, SyntaxError, TypeError) as e:
        logger.debug(f"Error parsing config: {e}")

    # Determine backend from test parameters
    backend = None
    if config and 'parameters' in config:
        params = config['parameters']
        if 'ttnn' in params:
            backend = Backend.ttnn
        elif 'ttmetal' in params:
            backend = Backend.ttmetal

    # Determine test status
    status = None
    if skipped:
        status = None  # Skipped tests don't have a specific status in the enum
    elif failed or error:
        if "compile" in (error_message or "").lower():
            status = TestStatus.compile_failed
        elif "run" in (error_message or "").lower():
            status = TestStatus.run_failed
        elif "golden" in (error_message or "").lower():
            status = TestStatus.golden_failed
        else:
            status = TestStatus.run_failed  # Default for failures
    else:
        status = TestStatus.success

    # Extract operation information from test name and config
    op_name = test_case_name
    framework_op_name = test_case_name
    op_kind = "builder_op"  # Default for builder tests
    
    # Parse tensor information from test parameters if available
    inputs = []
    outputs = []
    if config and 'parameters' in config:
        params = config['parameters']
        # Look for shape information in parameters
        for param in params:
            if 'x' in param and param.replace('x', '').replace('128', '').replace('256', '').replace('512', '').isdigit():
                # This looks like a shape parameter
                shape_parts = param.split('x')
                if all(part.isdigit() for part in shape_parts):
                    shape = [int(part) for part in shape_parts]
                    # Determine data type from parameters
                    data_type = "f32"  # default
                    for p in params:
                        if p in ['f32', 'f16', 'bf16', 'i32', 'i16', 'i8']:
                            data_type = p
                            break
                    
                    tensor_desc = TensorDesc(
                        shape=shape,
                        data_type=data_type,
                        buffer_type="DRAM",  # default
                        layout="ROW_MAJOR",  # default
                        grid_shape=[1, 1]    # default
                    )
                    inputs.append(tensor_desc)
                    outputs.append(tensor_desc)  # Assume same output shape for now
                break

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
            git_sha=None,  # Not available in pytest XML
            status=status,
            card_type=card_type,
            backend=backend,
        )
    except ValidationError as e:
        failure_happened()
        logger.error(f"Validation error: {e}")
        return None


def is_valid_testcase_(testcase):
    """
    Some cases of invalid tests include:

    - GitHub times out pytest so it records something like this:
        </testcase>
        <testcase time="0.032"/>
    """
    if "name" not in testcase.attrib or "classname" not in testcase.attrib:
        # This should be able to capture all cases where there's no info
        logger.warning("Found invalid test case with: no name nor classname")
        return False
    else:
        return True