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


class TestExpectedErrorMasking:
    """A TT_FATAL a test declared expected is masked before the analysis.yaml
    crash patterns scan; an undeclared error in the same block still crashes.
    The first two cases have no timestamps and exercise the fallback (mask only
    inside the bracket); the rest exercise the timestamp-gated window."""

    def test_declared_error_is_masked(self, tmp_path):
        log = (
            '[EXPECTED_ERROR BEGIN] RuntimeError message="must be BFLOAT8_B"\n'
            "TT_FATAL global_tensor must be BFLOAT8_B or BFLOAT16, got Float32\n"
            '[EXPECTED_ERROR END] RuntimeError message="must be BFLOAT8_B"\n'
        )
        assert not _extract(tmp_path, log).has_crash

    def test_undeclared_error_in_block_still_crashes(self, tmp_path):
        log = (
            '[EXPECTED_ERROR BEGIN] RuntimeError message="must be BFLOAT8_B"\n'
            "TT_FATAL unrelated device hang\n"
            '[EXPECTED_ERROR END] RuntimeError message="must be BFLOAT8_B"\n'
        )
        assert _extract(tmp_path, log).has_crash

    # Buffer-flush races interleave the C++ TT_FATAL just outside its Python markers.
    def test_error_flushed_before_begin_is_masked(self, tmp_path):
        log = (
            "2026-06-19 08:13:19.562 | critical | TT_FATAL must be BFLOAT8_B or BFLOAT16\n"
            '2026-06-19 08:13:19.561 | INFO | [EXPECTED_ERROR BEGIN] RuntimeError message="must be BFLOAT8_B"\n'
            '2026-06-19 08:13:19.563 | INFO | [EXPECTED_ERROR END] RuntimeError message="must be BFLOAT8_B"\n'
        )
        assert not _extract(tmp_path, log).has_crash

    def test_error_flushed_after_end_is_masked(self, tmp_path):
        log = (
            '2026-06-19 08:13:19.561 | INFO | [EXPECTED_ERROR BEGIN] RuntimeError message="must be BFLOAT8_B"\n'
            '2026-06-19 08:13:19.563 | INFO | [EXPECTED_ERROR END] RuntimeError message="must be BFLOAT8_B"\n'
            "2026-06-19 08:13:19.562 | critical | TT_FATAL must be BFLOAT8_B or BFLOAT16\n"
        )
        assert not _extract(tmp_path, log).has_crash

    def test_same_millisecond_error_is_masked(self, tmp_path):
        log = (
            '2026-06-19 08:13:19.561 | INFO | [EXPECTED_ERROR BEGIN] RuntimeError message="must be BFLOAT8_B"\n'
            "2026-06-19 08:13:19.561 | critical | TT_FATAL must be BFLOAT8_B or BFLOAT16\n"
            '2026-06-19 08:13:19.561 | INFO | [EXPECTED_ERROR END] RuntimeError message="must be BFLOAT8_B"\n'
        )
        assert not _extract(tmp_path, log).has_crash

    def test_error_inside_bracket_but_outside_time_span_still_crashes(self, tmp_path):
        # Physically between the markers, but timestamped outside their span — a late
        # flush from elsewhere, not this expected error.
        log = (
            '2026-06-19 08:13:19.561 | INFO | [EXPECTED_ERROR BEGIN] RuntimeError message="must be BFLOAT8_B"\n'
            "2026-06-19 08:13:25.000 | critical | TT_FATAL must be BFLOAT8_B or BFLOAT16\n"
            '2026-06-19 08:13:19.563 | INFO | [EXPECTED_ERROR END] RuntimeError message="must be BFLOAT8_B"\n'
        )
        assert _extract(tmp_path, log).has_crash

    def test_match_beyond_window_still_crashes(self, tmp_path):
        filler = "".join(f"2026-06-19 08:13:19.562 | INFO | noise {i}\n" for i in range(6))
        log = (
            '2026-06-19 08:13:19.561 | INFO | [EXPECTED_ERROR BEGIN] RuntimeError message="must be BFLOAT8_B"\n'
            '2026-06-19 08:13:19.563 | INFO | [EXPECTED_ERROR END] RuntimeError message="must be BFLOAT8_B"\n'
            + filler
            + "2026-06-19 08:13:19.562 | critical | TT_FATAL must be BFLOAT8_B or BFLOAT16\n"
        )
        assert _extract(tmp_path, log).has_crash
