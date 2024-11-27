# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

from generate_data import create_pipeline_json
import os
import json
import pytest


@pytest.mark.parametrize("run_id", ["11236784732", "12007373278"])
def test_create_pipeline_json(run_id):
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

    # assert pipeline json file has the correct
    with open(filename, "r") as file:
        data = json.load(file)
        assert data["jobs"][0]["card_type"] in ["N300", "N150", "E150"]

    # validate constrains
    assert check_constraint(pipeline)


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
