# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""The producer/parser pair for output names, shared by both stages."""

import pytest

from common.artifact_names import job_id_from_stem, qualified_stem


class TestQualifiedStem:
    def test_all_segments(self):
        assert qualified_stem("ai_job_summary", "5", 2, "77") == "ai_job_summary_r5_a2_j77"

    def test_first_attempt_is_labelled_too(self):
        assert qualified_stem("ai_job_summary", "5", 1, "77") == "ai_job_summary_r5_a1_j77"

    @pytest.mark.parametrize(
        "run_id,attempt,job_id,expected",
        [
            ("", None, "77", "ai_job_summary_j77"),
            ("5", None, "", "ai_job_summary_r5"),
            ("", None, "", "ai_job_summary"),
            ("5", 3, "", "ai_job_summary_r5_a3"),
        ],
    )
    def test_absent_segments_are_dropped(self, run_id, attempt, job_id, expected):
        assert qualified_stem("ai_job_summary", run_id, attempt, job_id) == expected

    def test_run_stage_prefix(self):
        assert qualified_stem("ai_run_summary", "5", 2) == "ai_run_summary_r5_a2"


class TestJobIdFromStem:
    @pytest.mark.parametrize(
        "stem,expected",
        [
            ("ai_job_summary_r5_a2_j99999", "99999"),
            ("ai_job_summary_j77", "77"),
            # No job segment: the tail is the attempt, which must not read as an id.
            ("ai_job_summary_r5_a2", ""),
            ("ai_job_summary_r5", ""),
            ("ai_job_summary", ""),
            # Infra stubs are keyed by a hash of the leg name, not a job id.
            ("ai_job_summary_3c1f9ab2de4f5061", ""),
        ],
    )
    def test_only_a_trailing_job_segment_counts(self, stem, expected):
        assert job_id_from_stem(stem) == expected

    def test_round_trips_the_builder(self):
        assert job_id_from_stem(qualified_stem("ai_job_summary", "5", 2, "77")) == "77"
