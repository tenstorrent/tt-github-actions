# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import pytest
import os
from parsers.tt_xla_op_by_op_parser import TTXlaOpByOpParser
from typing import Any

# Path where all test reports for testing are stored
REPORTS_PATH: str = "test/data/xla_op_by_op_reports/"


@pytest.mark.parametrize(
    "dirname,expected",
    [
        ("op-by-op-report-0-86083933793", {"num_tests": 340}),
        ("op-by-op-report-1-86083933787", {"num_tests": 83}),
        ("op-by-op-report-2-86083933790", {"num_tests": 214}),
    ],
)
def test_xla_op_by_op_parser(dirname: str, expected: dict[str, Any]):
    filepath = os.path.join(REPORTS_PATH, dirname)
    parser = TTXlaOpByOpParser()
    assert parser.can_parse(filepath)
    tests = parser.parse(filepath)
    assert len(tests) == expected["num_tests"]
