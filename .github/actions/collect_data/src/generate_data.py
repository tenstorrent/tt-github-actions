# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import os
import json
import argparse
from loguru import logger
from utils import get_github_runner_environment
from cicd import create_cicd_json_for_data_analysis, get_cicd_json_filename
from benchmark import create_json_from_report, get_benchmark_filename
from optests import create_optest_reports, get_optest_filename
from shared import is_failure


def create_pipeline_json(workflow_filename: str, jobs_filename: str, workflow_outputs_dir):

    github_runner_environment = get_github_runner_environment()
    pipeline = create_cicd_json_for_data_analysis(
        workflow_outputs_dir,
        github_runner_environment,
        workflow_filename,
        jobs_filename,
    )

    report_filename = get_cicd_json_filename(pipeline)
    logger.info(f"Writing pipeline JSON to {report_filename}")

    with open(report_filename, "w") as f:
        f.write(pipeline.model_dump_json())

    return pipeline, report_filename


def create_benchmark_jsons(pipeline, workflow_outputs_dir):
    results = []
    reports = create_json_from_report(pipeline, workflow_outputs_dir)
    for report in reports:
        report_filename = get_benchmark_filename(
            report
        )  # f"benchmark_{report.github_job_id}_{report.run_start_ts}.json"
        logger.info(f"Writing benchmark JSON to {report_filename}")
        with open(report_filename, "w") as f:
            f.write(report.model_dump_json())
        results.append((report, report_filename))
    return results


def create_optest_json(pipeline, workflow_outputs_dir):
    reports = create_optest_reports(pipeline, workflow_outputs_dir)
    for job_id, optests in reports:
        report_filename = get_optest_filename(pipeline, job_id)
        logger.info(f"Writing OpTest JSON to {report_filename}")
        with open(report_filename, "w") as f:
            f.write("[")
            for i, optest in enumerate(optests):
                if i > 0:
                    f.write(",")
                f.write(optest.model_dump_json())
            f.write("]")
    return reports


if __name__ == "__main__":

    global report_failure
    report_failure = False

    parser = argparse.ArgumentParser()
    parser.add_argument("--run_id", type=str, required=True, help="Run ID of the workflow")
    parser.add_argument(
        "--output_dir",
        type=str,
        required=False,
        default="generated/cicd",
        help="Output directory for the pipeline json",
    )
    args = parser.parse_args()

    logger.info(f"Creating pipeline JSON for workflow run ID {args.run_id}")
    pipeline, _ = create_pipeline_json(
        workflow_filename=f"{args.output_dir}/{args.run_id}/workflow.json",
        jobs_filename=f"{args.output_dir}/{args.run_id}/workflow_jobs.json",
        workflow_outputs_dir=args.output_dir,
    )

    logger.info(f"Creating benchmark JSON for workflow run ID {args.run_id}")
    create_benchmark_jsons(
        pipeline=pipeline,
        workflow_outputs_dir=args.output_dir,
    )

    logger.info(f"Creating OpTest JSON for workflow run ID {args.run_id}")
    create_optest_json(
        pipeline=pipeline,
        workflow_outputs_dir=args.output_dir,
    )

    if is_failure():
        raise Exception("Failed to generate some reports")
