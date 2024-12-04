# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import pytest
from parsers.tt_torch_model_tests_parser import TTTorchModelTestsParser


@pytest.mark.parametrize(
    "tar, expected",
    [
        ("run2.tar", {"tests_cnt": 32}),
    ],
)
def test_tt_torch_model_tests_parser(tar, expected):
    filepath = f"test/data/tt_torch_models/{tar}"
    parser = TTTorchModelTestsParser()
    assert parser.can_parse(filepath)
    tests = parser.parse(filepath)
    assert len(tests) == expected["tests_cnt"]
