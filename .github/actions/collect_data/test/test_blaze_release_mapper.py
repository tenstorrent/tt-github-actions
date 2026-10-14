# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""A published Blaze release (tt-shield's report_blaze-release_<job_id>.json): one benchmark_run per released leaf."""
import copy
import json
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock

import shared
from benchmark import BlazeReleaseDataMapper, _get_mapper, create_json_from_report


PUBLISHED_AT = datetime(2026, 10, 9, 16, 18, 55, tzinfo=timezone.utc)
RELEASE = {
    "version": "2026.10.1",
    "tag": "blaze-2026.10.1",
    "published_at": "2026-10-09T16:18:55Z",
    "url": "https://github.com/tenstorrent/tt-shield/releases/tag/blaze-2026.10.1",
    "commit": "9ccb969ed5d6f0355694ac307a95ddc2a068a1c1",
    "built_by_run": "https://github.com/tenstorrent/tt-shield/actions/runs/37953870685",
    "release_branch": "release-2026.10.1",
    "release_input_tag": "blaze-2026.10.1-input",
    "record_schema": "tt-shield/blaze-release/1",
    "record_sha256": "d6818fbc3f8688017e6cedc0bdbc340819b040576bdb099bbe6d8d81ddc6edc3",
}
GLM = {
    "leaf": "glm-5.3@quad+SC20",
    "model": "zai-org/GLM-5.3",
    "model_display_name": "GLM-5.3",
    "catalog_id": "glm-5.3",
    "model_version": "0.2.0-beta.1",
    "status": "EXPERIMENTAL",
    "impl": "blaze",
    "stack": "dgen",
    "capability": "disaggregated-prefill",
    "spec_decode": False,
    "device": {"decode": "SC20", "prefill": "quad"},
    "package": "glm-5.3@0.2.0-beta.1",
    "package_reference": "oci://ghcr.io/tenstorrent/tt-shield/charts/glm-5.3:0.2.0-beta.1",
    "package_digest": "sha256:" + "d" * 64,
    "package_verified": True,
    "chart": {"repo": "tenstorrent/tt-orchestration", "path": "charts/p", "ref": "branch", "commit": "c" * 40},
    "images": ["ghcr.io/tenstorrent/tt-shield/tt-blaze:glm-5.3-0.2.0-beta.1@sha256:" + "b" * 64],
    "deployments": [
        {
            "source": "observed",
            "deploy_run_id": 37477924052,
            "deploy_job_id": 112323392907,
            "helm_release": "pdg-g53-a9",
            "tests": [{"run_id": 37477924052, "job_id": 112348953986, "test": "sanity", "conclusion": "success"}],
        },
        {
            "source": "manual",
            "deploy_run_id": None,
            "deploy_job_id": None,
            "helm_release": None,
            "tests": [
                {
                    "run_id": 37581883830,
                    "job_id": 112673119739,
                    "test": "inference-workflow-agentic-traces",
                    "conclusion": "success",
                }
            ],
        },
    ],
    "test_job_ids": [112348953986, 112673119739],
}
MINIMAX = {
    **GLM,
    "leaf": "minimax-m3@SC16",
    "model": "MiniMaxAI/MiniMax-M3",
    "catalog_id": "minimax-m3",
    "model_version": "0.1.0",
    "device": {"decode": "SC16"},
    "deployments": GLM["deployments"][:1],
    "test_job_ids": [112348953986],
}
REPORT = {
    "report_type": "blaze_release",
    "schema": "tt-shield/blaze-release-db/1",
    "release": RELEASE,
    "leaves": [GLM, MINIMAX],
}


@pytest.fixture
def pipeline():
    pipeline = MagicMock()
    pipeline.pipeline_start_ts = "2026-10-14T07:00:00Z"
    pipeline.pipeline_end_ts = "2026-10-14T07:05:00Z"
    pipeline.project = "tt-shield"
    pipeline.git_commit_hash = "a" * 40
    pipeline.pipeline_submission_ts = "2026-10-14T06:59:00Z"
    pipeline.git_branch_name = "main"
    pipeline.github_pipeline_id = 40000000001
    pipeline.github_pipeline_link = "https://github.com/tenstorrent/tt-shield/actions/runs/40000000001"
    pipeline.git_author = "Danilo Cvijetic"
    pipeline.jobs = [
        MagicMock(
            github_job_id=7,
            job_start_ts="2026-10-14T07:01:00Z",
            job_end_ts="2026-10-14T07:02:00Z",
            docker_image=None,
            host_name="GitHub Actions 1009747413",
        )
    ]
    return pipeline


@pytest.fixture(autouse=True)
def clean_failure_flag():
    shared.reset_failure()
    yield
    shared.reset_failure()


def mapped(pipeline, report=None):
    return BlazeReleaseDataMapper().map_benchmark_data(pipeline, 7, copy.deepcopy(report or REPORT))


def test_the_report_type_selects_the_mapper_for_tt_shield():
    assert isinstance(_get_mapper("tt-shield", {"report_type": "blaze_release"}), BlazeReleaseDataMapper)


def test_one_run_per_released_leaf(pipeline):
    runs = mapped(pipeline)
    assert [run.ml_model_name for run in runs] == ["zai-org/GLM-5.3", "MiniMaxAI/MiniMax-M3"]
    assert not shared.is_failure()


def test_a_run_is_typed_dated_and_placed_as_the_release(pipeline):
    run = mapped(pipeline)[0]
    assert run.run_type == "blaze_release"
    assert run.ml_model_type == "LLM"
    # The publication, not the collection: a later dispatch records the same release time and commit.
    assert run.run_start_ts == PUBLISHED_AT and run.run_end_ts == PUBLISHED_AT
    assert run.git_commit_hash == RELEASE["commit"]
    assert run.device_info == {"device_name": "SUPER_CLUSTER", "roles": {"decode": "SC20", "prefill": "quad"}}


def test_a_run_keys_on_the_job_that_wrote_the_report(pipeline):
    run = mapped(pipeline)[0]
    assert run.github_job_id == 7
    assert run.github_pipeline_id == 40000000001
    assert run.git_repo_name == "tt-shield"
    assert run.device_hostname == "GitHub Actions 1009747413"
    assert run.docker_image is None
    assert (run.batch_size, run.input_sequence_length, run.output_sequence_length, run.dataset_name) == (
        None,
        None,
        None,
        None,
    )


def test_config_params_hold_the_release_and_the_leaf_but_not_the_model_again(pipeline):
    run = mapped(pipeline)[0]
    expected = {"release": RELEASE, **{k: v for k, v in GLM.items() if k != "model"}}
    assert run.config_params == expected


def test_measurements_count_the_deployments_and_test_jobs(pipeline):
    run = mapped(pipeline)[0]
    assert {(m.step_name, m.name, m.value) for m in run.measurements} == {
        ("blaze_release", "deployments", 2.0),
        ("blaze_release", "test_jobs", 2.0),
    }


def test_without_a_release_commit_the_pipeline_commit_stays(pipeline):
    report = copy.deepcopy(REPORT)
    report["release"]["commit"] = None
    assert mapped(pipeline, report)[0].git_commit_hash == "a" * 40


def test_a_leaf_without_an_hf_id_is_skipped_and_flagged(pipeline):
    report = copy.deepcopy(REPORT)
    report["leaves"][0]["model"] = "GLM-5.3"
    assert [run.ml_model_name for run in mapped(pipeline, report)] == ["MiniMaxAI/MiniMax-M3"]
    assert shared.is_failure()


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(schema="tt-shield/blaze-release-db/2"),
        lambda r: r["release"].pop("published_at"),
        lambda r: r.update(leaves=[]),
    ],
    ids=["unknown schema", "no publication time", "no leaves"],
)
def test_a_report_the_mapper_cannot_read_gives_no_runs_and_is_flagged(pipeline, change):
    report = copy.deepcopy(REPORT)
    change(report)
    assert mapped(pipeline, report) == []
    assert shared.is_failure()


def test_an_unknown_job_gives_nothing(pipeline):
    assert BlazeReleaseDataMapper().map_benchmark_data(pipeline, 999, copy.deepcopy(REPORT)) is None


def test_the_report_file_is_found_and_mapped_end_to_end(pipeline, tmp_path):
    artifact = tmp_path / str(pipeline.github_pipeline_id) / "artifacts" / "report_blaze-release_7"
    artifact.mkdir(parents=True)
    (artifact / "report_blaze-release_7.json").write_text(json.dumps(REPORT))
    runs = create_json_from_report(pipeline, str(tmp_path))
    assert sorted(run.ml_model_name for run in runs) == ["MiniMaxAI/MiniMax-M3", "zai-org/GLM-5.3"]
    assert {run.run_type for run in runs} == {"blaze_release"}
