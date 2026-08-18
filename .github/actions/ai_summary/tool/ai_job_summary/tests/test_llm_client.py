# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for LLM client base-url handling."""

from common.llm_client import LLMResponse, _ensure_v1


def test_ensure_v1_appends_when_missing():
    assert _ensure_v1("https://litellm.cloud.tenstorrent.com") == "https://litellm.cloud.tenstorrent.com/v1"
    assert _ensure_v1("https://litellm.cloud.tenstorrent.com/") == "https://litellm.cloud.tenstorrent.com/v1"


def test_ensure_v1_idempotent_when_present():
    assert _ensure_v1("https://host/v1") == "https://host/v1"
    assert _ensure_v1("https://host/v1/") == "https://host/v1"


class TestFinishReason:
    """finish_reason distinguishes a cut-off response from a malformed one."""

    def test_length_marks_truncated(self):
        r = LLMResponse(content="{partial", model="m", finish_reason="length")
        assert r.truncated

    def test_stop_is_not_truncated(self):
        r = LLMResponse(content="{}", model="m", finish_reason="stop")
        assert not r.truncated

    def test_missing_finish_reason_is_not_truncated(self):
        assert not LLMResponse(content="{}", model="m").truncated
