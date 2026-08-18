# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Shared pytest fixtures and path constants.

Test strategy:
  - test_extraction.py: unit tests calling extract_log() on real fixture logs
  - test_status.py: unit tests for get_job_status() and apply_llm_status()
  - test_summarize.py: unit tests for prompt building, parsing, markdown formatting
  - test_cli.py: integration tests for the full CLI pipeline (config → extract → output)

Fixture log samples in fixtures/log_samples/ are real CI logs.
Mock LLM responses in fixtures/mock_responses/ are representative JSON outputs.
"""

from pathlib import Path

import pytest


FIXTURE_LOG_DIR = Path(__file__).parent / "fixtures" / "log_samples"
FIXTURE_RESP_DIR = Path(__file__).parent / "fixtures" / "mock_responses"


@pytest.fixture(autouse=True)
def _no_ambient_ci_env(monkeypatch):
    """Clear the GitHub env every test starts from.

    Output names and run metadata are derived from these at the point of use, so
    an ambient value makes a test assert one thing locally and another in CI.
    Tests that need them set them explicitly.
    """
    for var in (
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_REPOSITORY",
        "GITHUB_SERVER_URL",
        "GITHUB_REF",
        "GITHUB_EVENT_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
