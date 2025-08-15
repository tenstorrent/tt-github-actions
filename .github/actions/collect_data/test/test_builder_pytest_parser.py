# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import pytest
import os
from parsers.builder_pytest_parser import BuilderPytestParser
from typing import Any

# Path where all test reports for testing are stored
REPORTS_PATH: str = "test/data/builder_reports/"


@pytest.mark.parametrize("filename,expected", "binoptests.xml", {"num_tests": 33})
def test_builder_pytest_parser(filename: str, expected: dict[str, Any]):
    filepath = os.path.join(REPORTS_PATH, filename)
    parser = BuilderPytestParser()
    assert parser.can_parse(filepath)
    tests = parser.parse(filepath)
    assert len(tests) == expected["num_tests"]
