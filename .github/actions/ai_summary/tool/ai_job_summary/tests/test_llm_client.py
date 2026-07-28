# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for outbound prompt sanitization (WAF-trigger neutralization, etc.)."""

from common.llm_client import _neutralize_waf_triggers, _sanitize_prompt


def test_neutralize_breaks_path_traversal_sequence():
    out = _neutralize_waf_triggers("../opt/venv/lib/pydantic/_config.py:291")
    assert "../" not in out
    assert out == ".. /opt/venv/lib/pydantic/_config.py:291"


def test_neutralize_every_occurrence():
    assert "../" not in _neutralize_waf_triggers("a/../b/../c")


def test_neutralize_leaves_clean_text_unchanged():
    text = "AttributeError: module 'ttnn.transformer' has no attribute 'X'"
    assert _neutralize_waf_triggers(text) == text


def test_sanitize_prompt_applies_cleanups():
    assert "../" not in _sanitize_prompt("see ../opt/venv/x")
