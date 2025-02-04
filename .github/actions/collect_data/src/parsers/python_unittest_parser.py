# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
import xmltodict
from loguru import logger
from pydantic_models import Test
from datetime import datetime, timedelta
from typing import Optional
from .parser import Parser


class PythonUnittestParser(Parser):
    """Parser for python unitest report files."""

    def can_parse(self, filepath: str):
        return filepath.endswith(".xml")

    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ):
        return get_tests(filepath)


def get_tests(test_report_path):
    tests = []
    with open(test_report_path) as f:
        data = f.read()
        dict_data = xmltodict.parse(data)
        previous_test_end_ts = None

        testsuites = dict_data["testsuites"]["testsuite"]
        if not isinstance(testsuites, list):
            testsuites = [testsuites]

        # Workaround: If testcases does not have timestamp, try using timestamp from testsuite and add duration
        if "@timestamp" not in testsuites[0]["testcase"][0]:
            logger.warning("Timestamp not found in test report, using testsuite timestamp")
            previous_test_end_ts = testsuites[0].get("@timestamp", None)
            # if testsuites does not have timestamp, use current time
            if not previous_test_end_ts:
                logger.warning("Timestamp not found in testsuite, using current time")
                previous_test_end_ts = datetime.now().isoformat()

        if "@file" not in testsuites[0]["testcase"][0]:
            logger.warning("Filepath not found in test report.")

        for testsuite in testsuites:

            # testcases can be dict or list
            testcases = testsuite["testcase"]
            if not isinstance(testcases, list):
                testcases = [testcases]

            for testcase in testcases:
                message = None
                test_start_ts = testcase.get("@timestamp", previous_test_end_ts)
                duration = testcase["@time"]
                skipped = testcase.get("skipped", False)
                error = testcase.get("error", False)
                failure = testcase.get("failure", False)
                file = testcase.get("@file", "")
                if skipped:
                    message = testcase["skipped"]["@message"]
                if error:
                    message = testcase["error"]["@type"]
                    message += "\n" + testcase["error"]["@message"]
                    message += "\n" + testcase["error"]["#text"]
                if failure:
                    message = testcase["failure"]["@type"]
                    message += "\n" + testcase["failure"]["@message"]
                    message += "\n" + testcase["failure"]["#text"]

                # Workaround: Data team requres unique test_start_ts
                if previous_test_end_ts:
                    test_start_ts = max(test_start_ts, previous_test_end_ts)
                test_end_ts = add_time(test_start_ts, duration)

                test = Test(
                    test_start_ts=test_start_ts,
                    test_end_ts=test_end_ts,
                    test_case_name=testcase["@name"],
                    filepath=file,
                    category=testcase["@classname"],
                    group="unittest",
                    owner=None,
                    frontend=None,
                    model_name=None,
                    op_name=None,
                    framework_op_name=None,
                    op_kind=None,
                    error_message=message,
                    success=not (error or failure),
                    skipped=bool(skipped),
                    full_test_name=f"{file}::{testcase['@name']}",
                    config=None,
                    tags=None,
                )
                tests.append(test)
                previous_test_end_ts = test_end_ts
    return tests


def add_time(timestamp_iso, duration_seconds):
    parsed_timestamp = datetime.fromisoformat(timestamp_iso)
    delta = timedelta(seconds=float(duration_seconds))
    result = parsed_timestamp + delta
    return result.isoformat()
