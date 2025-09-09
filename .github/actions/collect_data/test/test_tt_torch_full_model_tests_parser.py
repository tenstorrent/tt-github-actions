# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import pytest
from parsers.python_pytest_parser import PythonPytestParser


@pytest.mark.parametrize(
    "tar, project, github_job_id, expected",
    [
        ("mnist.xml", "tt-torch", 5, {"tests_cnt": 2}),
    ],
)
def test_tt_torch_full_model_tests_parser(tar, project, github_job_id, expected):
    filepath = f"test/data/tt_torch_models/{tar}"
    parser = PythonPytestParser()
    assert parser.can_parse(filepath)
    tests = parser.parse(filepath, project=project, github_job_id=github_job_id)
    assert len(tests) == expected["tests_cnt"]


def test_tt_torch_full_model_tests_parser_custom_error_message():
    filepath = f"test/data/tt_torch_models/mnist_custom_error_message.xml"
    parser = PythonPytestParser()
    assert parser.can_parse(filepath)
    tests = parser.parse(filepath, project="tt-torch", github_job_id=5)
    assert len(tests) == 3
    # Check that if a custom error message is provided, it is used in a non-xfail/skip case
    assert tests[0].error_message is not None

    # Check that if a test is skipped, the error message pulls the pytest.skip message
    assert "[pytest.skip]" in tests[1].error_message

    # Check that if a test is skipped, the error message pulls the pytest.skip message
    # instead of the custom error message when both are present
    assert "[pytest.skip]" in tests[2].error_message
