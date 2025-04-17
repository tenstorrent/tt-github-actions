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
        ("12890516474", "test/data/12890516474/artifacts/forge-benchmark-e2e-mnist_35942438708.json"),
        ("14492364249", "test/data/14492364249/artifacts/benchmark_forge-fe_e2e_mnist_linear_32_32_40651588679.json"),
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


@pytest.mark.parametrize(
    "run_id, expected_project",
    [
        ("12890516474", "tt-forge-fe"),
        ("14492364249", "tt-forge-fe"),
    ],
)
def test_check_benchmark_project(run_id, expected_project):
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
            assert report_json.get("git_repo_name") == expected_project


def compare_benchmark(reported, expected):

    # Compare manually reported data with expected data
    assert expected.get("model") == reported.get("ml_model_name")
    assert expected.get("model_type") == reported.get("ml_model_type")
    assert expected.get("run_type") == reported.get("run_type")
    assert expected.get("config") == reported.get("config_params")
    assert expected.get("num_layers") == reported.get("num_layers")
    assert expected.get("batch_size") == reported.get("batch_size")
    assert expected.get("precision") == reported.get("precision")
    assert expected.get("dataset_name") == reported.get("dataset_name")
    assert expected.get("profile_name") == reported.get("profiler_name")
    assert expected.get("input_sequence_length") == reported.get("input_sequence_length")
    assert expected.get("output_sequence_length") == reported.get("output_sequence_length")
    assert expected.get("image_dimension") == reported.get("image_dimension")
    assert expected.get("perf_analysis") == reported.get("perf_analysis")
    assert expected.get("training") == reported.get("training")
    assert expected.get("device_ip") == reported.get("device_ip")
    for i, measurement in enumerate(expected.get("measurements", [])):
        assert measurement.get("iteration") == reported["measurements"][i].get("iteration")
        assert measurement.get("step_name") == reported["measurements"][i].get("step_name")
        assert measurement.get("step_warm_up_num_iterations") == reported["measurements"][i].get(
            "step_warm_up_num_iterations"
        )
        assert measurement.get("measurement_name") == reported["measurements"][i].get("name")
        assert measurement.get("value") == reported["measurements"][i].get("value")
        assert measurement.get("target") == reported["measurements"][i].get("target")
        assert measurement.get("device_power") == reported["measurements"][i].get("device_power")
        assert measurement.get("device_temperature") == reported["measurements"][i].get("device_temperature")


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
