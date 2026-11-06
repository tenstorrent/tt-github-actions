# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""agentic-models perf reports (report_perf_<stage>_<job_id>.json) use the Forge report shape."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from benchmark import ForgeBenchmarkDataMapper, _get_mapper, _map_benchmark_data

START = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc)


def pipeline():
    p = MagicMock()
    p.project = "agentic-models"
    p.pipeline_start_ts = START
    p.pipeline_end_ts = END
    p.pipeline_submission_ts = START
    p.git_commit_hash = "abc123"
    p.git_branch_name = "main"
    p.github_pipeline_id = 42
    p.github_pipeline_link = "https://github.com/tenstorrent/agentic-models/actions/runs/42"
    p.git_author = "someone"
    p.jobs = [MagicMock(github_job_id=7, job_start_ts=START, job_end_ts=END, docker_image=None, host_name="runner-1")]
    return p


def test_agentic_models_uses_the_forge_mapper():
    assert isinstance(_get_mapper("agentic-models", {}), ForgeBenchmarkDataMapper)


def test_agentic_models_benchmark_report_maps_to_one_run_with_its_config():
    report = {
        "run_type": "pd_benchmark",
        "model": "tencent/Hy3",
        "dataset_name": "smoke",
        "batch_size": 1,
        "input_sequence_length": 128,
        "output_sequence_length": 32,
        "config": {"deployment": "hy3 / 1p1d", "layout": "1p1d", "temperature": 0},
        "measurements": [
            {"iteration": 1, "step_name": "smoke", "measurement_name": "mean_ttft_ms", "value": 812.5},
            {"iteration": 1, "step_name": "smoke", "measurement_name": "output_throughput", "value": 31.2},
        ],
    }
    (run,) = _map_benchmark_data(pipeline(), 7, report)
    assert run.git_repo_name == "agentic-models"
    assert run.ml_model_name == "tencent/Hy3"
    assert run.config_params["layout"] == "1p1d"
    assert run.device_hostname == "runner-1"
    assert [(m.name, m.value) for m in run.measurements] == [("mean_ttft_ms", 812.5), ("output_throughput", 31.2)]
