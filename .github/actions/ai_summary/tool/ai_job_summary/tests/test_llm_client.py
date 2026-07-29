# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for LLM client base-url handling."""

from common.llm_client import _ensure_v1


def test_ensure_v1_appends_when_missing():
    assert _ensure_v1("https://litellm.cloud.tenstorrent.com") == "https://litellm.cloud.tenstorrent.com/v1"
    assert _ensure_v1("https://litellm.cloud.tenstorrent.com/") == "https://litellm.cloud.tenstorrent.com/v1"


def test_ensure_v1_idempotent_when_present():
    assert _ensure_v1("https://host/v1") == "https://host/v1"
    assert _ensure_v1("https://host/v1/") == "https://host/v1"
