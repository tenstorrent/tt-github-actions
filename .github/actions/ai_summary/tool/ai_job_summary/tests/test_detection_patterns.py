# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Tests for detection_patterns (has_crash / has_timeout), sourced from
analysis.yaml. The golden test locks the bundled patterns to the values
previously hardcoded in extract.py — guarding behavior-neutrality.
"""

from ai_job_summary.config import load_config
from ai_job_summary.extract import extract_log

# Verbatim from the pre-refactor extract.py literals.
GOLDEN = {
    "crash": ["TT_FATAL", "TT_THROW", r"\bpanic\b", "Segmentation fault", "SIGSEGV"],
    "crash_python": [
        r"(?<![\w.])(?:AttributeError|KeyError|RuntimeError|ModuleNotFoundError|ImportError):",
        r"\bERROR\s+collecting\b",
    ],
    "crash_killed": [r"\b\d+\s+Killed\b", r"\bSIGKILL\b", r"\bSIGTERM\b"],
    "timeout": [
        r"##\[error\].*timed\s*out",
        r"(?:job|test|request|process|operation)\s+timed\s*out",
        r"timed\s*out\s+(?:after|waiting)",
        r"exceeded\s+time\s+limit",
        r"cancelled\s+due\s+to\s+timeout",
    ],
}


def _extract(tmp_path, text, **kw):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "run.log").write_text(text)
    return extract_log(d, **kw)


class TestGolden:
    def test_bundled_matches_pre_refactor_literals(self):
        assert load_config()["detection_patterns"] == GOLDEN


class TestDefaultDetection:
    def test_tt_fatal_is_crash(self, tmp_path):
        assert _extract(tmp_path, "TT_FATAL something\n").has_crash

    def test_lowercase_tt_fatal_is_crash(self, tmp_path):
        # crash group is case-insensitive
        assert _extract(tmp_path, "tt_fatal blah\n").has_crash

    def test_runtime_error_is_crash(self, tmp_path):
        assert _extract(tmp_path, "RuntimeError: boom\n").has_crash

    def test_module_qualified_error_is_not_crash(self, tmp_path):
        # lookbehind: vllm.RuntimeError: must not match
        assert not _extract(tmp_path, "vllm.RuntimeError: boom\n").has_crash

    def test_lowercase_sigkill_is_not_crash(self, tmp_path):
        # killed group is case-sensitive
        assert not _extract(tmp_path, "process sigkill noise\n").has_crash

    def test_timed_out_is_timeout(self, tmp_path):
        assert _extract(tmp_path, "##[error] job timed out\n").has_timeout

    def test_timeout_config_value_is_not_timeout(self, tmp_path):
        assert not _extract(tmp_path, "timeout: 60s\n").has_timeout

    def test_clean_log_no_signals(self, tmp_path):
        e = _extract(tmp_path, "INFO: all good\n")
        assert not e.has_crash and not e.has_timeout


class TestOverlay:
    def test_project_extends_crash_group_additively(self):
        cfg = load_config({"detection_patterns": {"crash": ["MY_FATAL"]}})
        assert cfg["detection_patterns"]["crash"] == GOLDEN["crash"] + ["MY_FATAL"]
        # other groups untouched
        assert cfg["detection_patterns"]["timeout"] == GOLDEN["timeout"]

    def test_custom_pattern_detected(self, tmp_path):
        det = {"crash": GOLDEN["crash"] + ["MY_CUSTOM_FATAL"]}
        assert _extract(tmp_path, "MY_CUSTOM_FATAL hit\n", detection_patterns=det).has_crash
