# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import pytest
import json
import tempfile
from pathlib import Path
from parsers.parameter_support_test_parser import ParameterSupportTestParser


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
    parser = ParameterSupportTestParser()
    assert parser.can_parse(sample_parameter_support_json) is True


def test_cannot_parse_non_json():
    parser = ParameterSupportTestParser()
    assert parser.can_parse("test.xml") is False


def test_parse_parameter_support_tests(sample_parameter_support_json):
    parser = ParameterSupportTestParser()
    tests = parser.parse(sample_parameter_support_json)

    assert len(tests) == 3

    assert tests[0].config["endpoint_url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert tests[0].config["model_name"] == "Llama-3.1-8B-Instruct"
    assert tests[0].config["model_impl"] == "tt-transformers"
    assert tests[0].test_case_name == "test_n[2]"
    assert tests[0].success is False
    assert tests[0].error_message == "Connection refused"
    assert tests[0].category == "parameter_support"
    assert tests[0].owner == "tt-shield"
    assert tests[0].group == "test_n"
    assert tests[0].tags["type"] == "parameter_support_test"

    assert tests[1].config["endpoint_url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert tests[1].config["model_name"] == "Llama-3.1-8B-Instruct"
    assert tests[1].config["model_impl"] == "tt-transformers"
    assert tests[1].test_case_name == "test_n[3]"
    assert tests[1].success is True
    assert tests[1].error_message is None
    assert tests[1].category == "parameter_support"
    assert tests[1].owner == "tt-shield"
    assert tests[1].group == "test_n"
    assert tests[1].tags["type"] == "parameter_support_test"

    assert tests[2].config["endpoint_url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert tests[2].config["model_name"] == "Llama-3.1-8B-Instruct"
    assert tests[2].config["model_impl"] == "tt-transformers"
    assert tests[2].test_case_name == "test_max_tokens[5]"
    assert tests[2].success is True
    assert tests[2].error_message is None
    assert tests[2].category == "parameter_support"
    assert tests[2].owner == "tt-shield"
    assert tests[2].group == "test_max_tokens"
    assert tests[2].tags["type"] == "parameter_support_test"


def test_parse_empty_results():
    data = {"parameter_support_tests": {"endpoint_url": "http://test", "model_name": "test_model", "results": {}}}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        temp_path = f.name

    try:
        parser = ParameterSupportTestParser()
        tests = parser.parse(temp_path)
        assert len(tests) == 0
    finally:
        Path(temp_path).unlink()
