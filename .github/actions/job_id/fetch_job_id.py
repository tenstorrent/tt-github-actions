# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
import argparse
import requests
import sys
import os


def fetch_job_id(job_name, repo, run_id, run_attempt):
    page = 1
    all_jobs = []
    while True:
        url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}&attempt_number={run_attempt}"
        headers = {"Authorization": f"token {os.environ.get('GH_TOKEN')}"}
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            print(f"Error: Failed to fetch jobs (HTTP {response.status_code})", file=sys.stderr)
            sys.exit(1)

        jobs = response.json().get("jobs", [])
        if not jobs:
            break

        all_jobs.extend(jobs)
        page += 1

    matching_jobs = [job["id"] for job in all_jobs if job_name in job["name"]]
    if len(matching_jobs) > 1:
        print("Error: More than one matching job found, job name must be unique", file=sys.stderr)
        print("MATCHING JOB NAMES:")
        for job in all_jobs:
            if job_name in job["name"]:
                print(job["name"])
        sys.exit(1)
    if len(matching_jobs) == 0:
        print("Error: Job ID not found", file=sys.stderr)
        print("JOB NAMES:")
        for job in all_jobs:
            print(job["name"])
        sys.exit(1)

    return matching_jobs[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch the job ID from GitHub Actions API.")
    parser.add_argument("--job-name", required=True, help="Name of the job")
    parser.add_argument("--repo", required=True, help="GitHub repository (e.g., owner/repo)")
    parser.add_argument("--run-id", required=True, help="GitHub workflow run ID")
    parser.add_argument("--run-attempt", required=True, help="GitHub workflow run attempt ID")

    args = parser.parse_args()

    job_id = fetch_job_id(job_name=args.job_name, repo=args.repo, run_id=args.run_id, run_attempt=args.run_attempt)
    print(job_id)
