# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from loguru import logger
from functools import partial
from pydantic_models import Test
from datetime import datetime
from typing import Optional
from .parser import Parser
from . import junit_xml_utils
from utils import parse_timestamp
import ast
import html
from pydantic import ValidationError
from shared import failure_happened


class PythonPytestParser(Parser):
    """Parser for python unitest report files."""

    def can_parse(self, filepath: str):
        if not filepath.endswith(".xml"):
            return False
        report_root_tree = junit_xml_utils.get_xml_file_root_element_tree(filepath)
        report_root = report_root_tree.getroot()
        is_pytest = junit_xml_utils.is_pytest_junit_xml(report_root)
        return is_pytest

    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ):
        return get_tests(filepath)


def get_tests(filepath):
    report_root_tree = junit_xml_utils.get_xml_file_root_element_tree(filepath)
    report_root = report_root_tree.getroot()
    testsuite = report_root[0]
    default_timestamp = parse_timestamp(testsuite.attrib["timestamp"])
    get_pydantic_test = partial(get_pydantic_test_from_pytest_testcase_, default_timestamp=default_timestamp)
    tests = []
    for testcase in testsuite:
        if is_valid_testcase_(testcase):
            tests.append(get_pydantic_test(testcase))
    return tests


def get_pydantic_test_from_pytest_testcase_(testcase, default_timestamp=datetime.now()):
    skipped = junit_xml_utils.get_pytest_testcase_is_skipped(testcase)
    failed = junit_xml_utils.get_pytest_testcase_is_failed(testcase)
    error = junit_xml_utils.get_pytest_testcase_is_error(testcase)
    success = not (failed or error)

    error_message = None

    # Error is a scarier thing than failure because it means there's an infra error, expose that first
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

    # Error at the beginning of a test can prevent pytest from recording timestamps at all
    if not (skipped or error) and "start_timestamp" in properties and "end_timestamp" in properties:
        test_start_ts = parse_timestamp(properties["start_timestamp"])
        test_end_ts = parse_timestamp(properties["end_timestamp"])
    else:
        test_start_ts = default_timestamp
        test_end_ts = default_timestamp

    test_case_name = testcase.attrib["name"].split("[")[0]

    filepath_no_ext = testcase.attrib["classname"].replace(".", "/")
    filepath = f"{filepath_no_ext}.py"

    def get_category_from_pytest_testcase_(testcase_):
        # TODO Adjust to project specific test categories
        categories = ["models", "ttnn", "tt_eager", "tt_metal"]
        for category in categories:
            if category in testcase_.attrib["classname"]:
                return category
        return "other"

    category = get_category_from_pytest_testcase_(testcase)

    # leaving empty for now
    group = properties.get("group")

    # leaving empty for now
    owner = properties.get("owner")

    full_test_name = f"{filepath}::{testcase.attrib['name']}"

    # to be populated with [] if available
    config = None
    tags = None

    try:
        tag_string = properties.get("tags")
        if tag_string is not None:
            tags = ast.literal_eval(html.unescape(tag_string))
    except (ValueError, SyntaxError, TypeError) as e:
        print(f"Error parsing tags: {e}")

    try:
        config_string = properties.get("config")
        if config_string is not None:
            config = ast.literal_eval(html.unescape(config_string))
    except (ValueError, SyntaxError, TypeError) as e:
        print(f"Error parsing config: {e}")

    try:
        return Test(
            test_start_ts=test_start_ts,
            test_end_ts=test_end_ts,
            test_case_name=test_case_name,
            filepath=filepath,
            category=category,
            group=group,
            owner=owner,
            error_message=error_message,
            success=success,
            skipped=skipped,
            full_test_name=full_test_name,
            config=config,
            tags=tags,
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
