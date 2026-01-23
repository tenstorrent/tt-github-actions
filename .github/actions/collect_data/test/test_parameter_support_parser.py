# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import pytest
import json
import tempfile
from pathlib import Path
from parsers.parameter_support_parser import ParameterSupportParser


@pytest.fixture
def sample_parameter_support_json():
    """Create a sample parameter support test JSON file."""
    data = {
        "parameter_support_tests": {
            "endpoint_url": "http://127.0.0.1:8000/v1/chat/completions",
            "model_name": "Llama-3.1-8B-Instruct",
            "model_impl": "tt-transformers",
            "results": {
                "test_n": [
                    {"status": "failed", "message": "Connection refused", "test_node_name": "test_n[2]"},
                    {"status": "passed", "message": "", "test_node_name": "test_n[3]"},
                ],
                "test_max_tokens": [
                    {"status": "passed", "message": "max_tokens=2048 supported", "test_node_name": "test_max_tokens[5]"}
                ],
            },
        }
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        temp_path = f.name

    yield temp_path

    Path(temp_path).unlink()


def test_can_parse_parameter_support_json(sample_parameter_support_json):
    parser = ParameterSupportParser()
    assert parser.can_parse(sample_parameter_support_json) is True


def test_cannot_parse_non_json():
    parser = ParameterSupportParser()
    assert parser.can_parse("test.xml") is False


def test_parse_parameter_support_tests(sample_parameter_support_json):
    parser = ParameterSupportParser()
    tests = parser.parse(sample_parameter_support_json)

    assert len(tests) == 3

    # Check first test (failed)
    assert tests[0].test_case_name == "test_n"
    assert tests[0].success is False
    assert tests[0].error_message == "Connection refused"
    assert tests[0].category == "parameter_support"
    assert tests[0].group == "test_n"
    assert tests[0].config["model_name"] == "Llama-3.1-8B-Instruct"
    assert tests[0].tags["type"] == "parameter_support_test"

    # Check second test (passed)
    assert tests[1].test_case_name == "test_n"
    assert tests[1].success is True
    assert tests[1].error_message is None
    assert tests[1].config["model_name"] == "Llama-3.1-8B-Instruct"
    # Check third test (passed)
    assert tests[2].test_case_name == "test_max_tokens"
    assert tests[2].success is True
    assert tests[2].config["model_name"] == "Llama-3.1-8B-Instruct"


def test_parse_empty_results():
    data = {"parameter_support_tests": {"endpoint_url": "http://test", "model_name": "test_model", "results": {}}}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        temp_path = f.name

    try:
        parser = ParameterSupportParser()
        tests = parser.parse(temp_path)
        assert len(tests) == 0
    finally:
        Path(temp_path).unlink()
