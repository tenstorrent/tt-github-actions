# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for LLM client base-url handling."""

import pytest

from types import SimpleNamespace
from unittest.mock import MagicMock

from common.llm_client import LLMClient, LLMResponse, _ensure_v1


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


class TestChatCarriesFinishReason:
    """finish_reason has to survive the SDK boundary, or truncation is invisible."""

    def _client_with(self, finish_reason):
        client = LLMClient(api_key="k", model="m")
        sdk = MagicMock()
        sdk.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason=finish_reason)],
            model="m",
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        )
        client._client = sdk
        return client

    def test_length_reaches_the_response(self):
        assert self._client_with("length").chat("p").truncated

    def test_stop_reaches_the_response(self):
        r = self._client_with("stop").chat("p")
        assert r.finish_reason == "stop"
        assert not r.truncated

    def test_absent_finish_reason_is_empty_not_none(self):
        assert self._client_with(None).chat("p").finish_reason == ""


def test_empty_choices_raises_instead_of_indexerror():
    client = LLMClient(api_key="k", model="m")
    sdk = MagicMock()
    sdk.chat.completions.create.return_value = SimpleNamespace(choices=[], model="m", usage=None)
    client._client = sdk
    with pytest.raises(RuntimeError, match="no choices"):
        client.chat("p")
