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


FIXTURE_LOG_DIR = Path(__file__).parent / "fixtures" / "log_samples"
FIXTURE_RESP_DIR = Path(__file__).parent / "fixtures" / "mock_responses"
