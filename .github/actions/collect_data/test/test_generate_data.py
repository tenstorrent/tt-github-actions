# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from generate_data import create_pipeline_json, create_benchmark_jsonl
import os
import json
import pytest
from deepdiff import DeepDiff


@pytest.mark.parametrize(
    "run_id, expected",
    [
        ("14439943793", {"jobs_cnt": 1, "tests_cnt": 0}),
    ],
)
def test_create_pipeline_json(run_id, expected):
    """
    End-to-end test for create_pipeline_json function
    Calling this will generate a pipeline json file
    """
    os.environ["GITHUB_EVENT_NAME"] = "test"
    pipeline, filename = create_pipeline_json(
        workflow_filename=f"test/data/{run_id}/workflow.json",
        jobs_filename=f"test/data/{run_id}/workflow_jobs.json",
        workflow_outputs_dir="test/data",
    )

    assert os.path.exists(filename)

    with open(filename, "r") as file:
        pipeline_json = json.load(file)

        # assert pipeline json has the correct card types
        expected_card_types = ["N300", "N150", "E150", None]
        for job in pipeline_json["jobs"]:
            assert job["card_type"] in expected_card_types
            assert "job_status" in job

        # assert pipeline json has the correct number of jobs and tests
        assert len(pipeline_json["jobs"]) == expected["jobs_cnt"]
        tests_cnt = 0
        for job in pipeline_json["jobs"]:
            tests_cnt += len(job["tests"])
        assert tests_cnt == expected["tests_cnt"]

        # asset skip_tests have error message set
        for job in pipeline_json["jobs"]:
            for test in job["tests"]:
                if test["skipped"]:
                    assert test["error_message"] is not None

    # validate constraints
    assert check_constraint(pipeline)


@pytest.mark.parametrize(
    "run_id, expected_file",
    [
        ("12890516473", "test/data/12890516473/expected/benchmark_40487698077.json"),
        ("14439943793", "test/data/14439943793/expected/benchmark_40487698077.jsonl"),
    ],
)
def test_create_benchmark_json(run_id, expected_file):
    """
    End-to-end test for create_pipeline_json function
    Calling this will generate a pipeline json file
    """
    os.environ["GITHUB_EVENT_NAME"] = "test"

    pipeline, _ = create_pipeline_json(
        workflow_filename=f"test/data/{run_id}/workflow.json",
        jobs_filename=f"test/data/{run_id}/workflow_jobs.json",
        workflow_outputs_dir="test/data",
    )
    report_file = create_benchmark_jsonl(pipeline, "test/data")
    assert os.path.exists(report_file)

    # Compare the content of the report file and the expected file as JSON objects
    with open(report_file, "r") as report, open(expected_file, "r") as expected:
        report_json = [json.loads(line) for line in report]
        expected_json = [json.loads(line) for line in expected]
        diff = DeepDiff(report_json, expected_json, exclude_regex_paths=[r".*step_start_ts.*"])

        # Assert no differences
        assert not diff, f"Objects are not equal. Differences: {diff}"


def check_constraint(pipeline):
    # check if the pipeline has the correct constraints
    # unique cicd_job_id, full_test_name, test_start_ts
    unique_tests = set()
    for job in pipeline.jobs:
        for test in job.tests:
            key = (job.github_job_id, test.full_test_name, test.test_start_ts)
            if key in unique_tests:
                raise ValueError("Job already exists: ", key)
            unique_tests.add(key)
    # unique cicd_pipeline_id, name, job_submission_ts, job_start_ts, job_end_ts
    unique_jobs = set()
    for job in pipeline.jobs:
        key = (pipeline.github_pipeline_id, job.name, job.job_submission_ts, job.job_start_ts, job.job_end_ts)
        if key in unique_jobs:
            raise ValueError("Job already exists: ", key)
        unique_jobs.add(key)
    return True
