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
