# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Flexible LLM client supporting multiple backends.

Supports:
- Tenstorrent internal LLMs (OpenAI-compatible via litellm proxy)
- OpenAI API
- Any OpenAI-compatible endpoint

Environment variables (checked in order):
    TT_CHAT_API_KEY + TT_CHAT_URL     Tenstorrent internal LLMs
    TT_CHAT_MODEL                     Model (default: anthropic/claude-sonnet-4-5-20250929)

    API_KEY + BASE_URL + MODEL        Generic OpenAI-compatible (from .env)

    OPENAI_API_KEY                    OpenAI API directly
"""

import os
import time
from dataclasses import dataclass

from openai import OpenAI, OpenAIError


@dataclass
class LLMResponse:
    """Response from LLM with usage metrics."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_time_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient:
    """OpenAI-compatible LLM client."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "gpt-4o",
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        """Lazy-load the OpenAI client."""
        if self._client is None:
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def chat(
        self,
        prompt: str,
        max_tokens: int = 4000,
        temperature: float = 0.0,
        timeout: float = 300.0,
    ) -> LLMResponse:
        """Send a chat message and get a response."""
        messages = [{"role": "user", "content": prompt}]

        start_time = time.time()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except OpenAIError as e:
            raise RuntimeError(f"LLM API error: {e}") from e
        response_time_ms = (time.time() - start_time) * 1000

        return LLMResponse(
            content=response.choices[0].message.content,
            model=response.model,
            prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
            completion_tokens=response.usage.completion_tokens if response.usage else 0,
            response_time_ms=response_time_ms,
        )

    @classmethod
    def from_env(cls) -> "LLMClient":
        """Create client from environment variables.

        Checks (in order):
        1. TT_CHAT_API_KEY + TT_CHAT_URL (Tenstorrent internal)
        2. API_KEY + BASE_URL + MODEL (generic, from .env)
        3. OPENAI_API_KEY (OpenAI direct)
        """
        # Check for TT internal LLM
        tt_api_key = os.environ.get("TT_CHAT_API_KEY")
        tt_url = os.environ.get("TT_CHAT_URL")
        if tt_api_key and tt_url:
            return cls(
                api_key=tt_api_key,
                base_url=tt_url,
                model=os.environ.get("TT_CHAT_MODEL", "anthropic/claude-sonnet-4-5-20250929"),
            )

        # Check for generic .env style
        api_key = os.environ.get("API_KEY")
        if api_key:
            return cls(
                api_key=api_key,
                base_url=os.environ.get("BASE_URL"),
                model=os.environ.get("MODEL", "gpt-4o"),
            )

        # Check for OpenAI direct
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            return cls(
                api_key=openai_key,
                model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            )

        raise ValueError(
            "No LLM credentials found. Set one of:\n"
            "  - TT_CHAT_API_KEY + TT_CHAT_URL (Tenstorrent internal)\n"
            "  - API_KEY + BASE_URL + MODEL (generic OpenAI-compatible)\n"
            "  - OPENAI_API_KEY (OpenAI direct)"
        )

    def __repr__(self):
        return f"LLMClient(model={self.model}, base_url={self.base_url})"


def get_llm_client() -> LLMClient:
    """Get an LLM client based on available environment variables."""
    return LLMClient.from_env()
