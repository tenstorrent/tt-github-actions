# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
import pytest

from ai_job_summary.config import load_config


class TestLoadConfigPassThrough:
    def test_log_dirs_passed_through(self):
        config = load_config({"log_dirs": ["a", "b"]})
        assert config["log_dirs"] == ["a", "b"]

    def test_model_passed_through(self):
        config = load_config({"model": "claude-test"})
        assert config["model"] == "claude-test"

    def test_output_dir_passed_through(self):
        config = load_config({"output_dir": "some/path"})
        assert config["output_dir"] == "some/path"

    def test_workspace_passed_through(self):
        config = load_config({"workspace": "/custom/workspace"})
        assert config["workspace"] == "/custom/workspace"

    def test_tool_dir_raises_error(self):
        with pytest.raises(ValueError, match="tool_dir is no longer supported"):
            load_config({"tool_dir": ".github/tools/ai-summary"})

    def test_categories_merged_not_passed_through(self):
        config = load_config({"categories": {"custom:cat": {"description": "test", "patterns": ["x"]}}})
        assert "custom:cat" in config["categories"]

    def test_layers_extended_by_default(self):
        bundled = load_config()
        bundled_layers_count = len(bundled["layers"])
        config = load_config({"layers": [{"name": "extra", "patterns": ["x"]}]})
        assert len(config["layers"]) == bundled_layers_count + 1
        assert config["layers"][-1]["name"] == "extra"

    def test_layers_replace_mode(self):
        config = load_config(
            {
                "layers": [{"name": "only", "patterns": ["x"]}],
                "layers_mode": "replace",
            }
        )
        assert len(config["layers"]) == 1
        assert config["layers"][0]["name"] == "only"

    def test_bundled_analysis_yaml_loaded_by_default(self):
        config = load_config()
        assert "vllm:engine" in config["categories"]
        assert "tt-metal:trace" in config["categories"]
        assert "runtime:exception" in config["categories"]
        # `model` is no longer in the bundled config; it lives in the
        # per-project config so consumers pick the LLM independently.
        assert "model" not in config

    def test_no_project_overlay_returns_bundled(self):
        config = load_config()
        # Defaults are populated for the analysis fields
        assert isinstance(config["categories"], dict)
        assert isinstance(config["layers"], list)
        assert isinstance(config["repos"], dict)
