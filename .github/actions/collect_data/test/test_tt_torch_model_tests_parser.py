# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import pytest
from parsers.tt_torch_model_tests_parser import TTTorchModelTestsParser


@pytest.mark.parametrize(
    "tar, project, github_job_id, expected",
    [
        ("run2.tar", "tt-torch", 5, {"tests_cnt": 32}),
    ],
)
def test_tt_torch_model_tests_parser(tar, project, github_job_id, expected):
    filepath = f"test/data/tt_torch_models/{tar}"
    parser = TTTorchModelTestsParser()
    assert parser.can_parse(filepath)
    tests = parser.parse(filepath, project=project, github_job_id=github_job_id)
    assert len(tests) == expected["tests_cnt"]
