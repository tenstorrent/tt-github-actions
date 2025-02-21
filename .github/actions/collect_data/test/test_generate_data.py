# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from generate_data import create_pipeline_json, create_benchmark_jsons
import os
import json
import pytest


@pytest.mark.parametrize(
    "run_id, expected",
    [
        ("11236784732", {"jobs_cnt": 2, "tests_cnt": 583}),
        ("12007373278", {"jobs_cnt": 9, "tests_cnt": 245}),
        ("12083382635", {"jobs_cnt": 21, "tests_cnt": 322}),
        ("12084081698", {"jobs_cnt": 4, "tests_cnt": 250}),
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
        ("12890516473", "test/data/12890516473/artifacts/forge-benchmark-e2e-mnist_35942438708.json"),
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
    reports = create_benchmark_jsons(pipeline, "test/data")
    for _, report_filename in reports:
        assert os.path.exists(report_filename)
        with open(report_filename, "r") as file:
            report_json = json.load(file)
            # load results json file and compare with report
            expected = json.load(open(f"{expected_file}", "r"))
            compare_benchmark(report_json, expected)


def compare_benchmark(reported, expected):

    # Compare manually reported data with expected data
    assert expected["model"] == reported["ml_model_name"]
    assert expected["model_type"] == reported["ml_model_type"]
    assert expected["run_type"] == reported["run_type"]
    assert expected["config"] == reported["config_params"]
    assert expected["num_layers"] == reported["num_layers"]
    assert expected["batch_size"] == reported["batch_size"]
    assert expected["precision"] == reported["precision"]
    assert expected["dataset_name"] == reported["dataset_name"]
    assert expected["profile_name"] == reported["profiler_name"]
    assert expected["input_sequence_length"] == reported["input_sequence_length"]
    assert expected["output_sequence_length"] == reported["output_sequence_length"]
    assert expected["image_dimension"] == reported["image_dimension"]
    assert expected["perf_analysis"] == reported["perf_analysis"]
    assert expected["training"] == reported["training"]
    assert expected["device_ip"] == reported["device_ip"]
    for i, measurement in enumerate(expected["measurements"]):
        assert measurement["iteration"] == reported["measurements"][i]["iteration"]
        assert measurement["step_name"] == reported["measurements"][i]["step_name"]
        assert measurement["step_warm_up_num_iterations"] == reported["measurements"][i]["step_warm_up_num_iterations"]
        assert measurement["measurement_name"] == reported["measurements"][i]["name"]
        assert measurement["value"] == reported["measurements"][i]["value"]
        assert measurement["target"] == reported["measurements"][i]["target"]
        assert measurement["device_power"] == reported["measurements"][i]["device_power"]
        assert measurement["device_temperature"] == reported["measurements"][i]["device_temperature"]


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
