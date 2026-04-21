# Collect data action

This action provides the following functionality:
- collect workflow information and test reports
- generate JSON file with workflow data
- upload JSON file to sftp server


## Basic usage

Add workflow that triggers on workflow_run completed event (when other pipelines finish)

```
on:
  workflow_run:
    workflows: # List workflow that we want to collect data for
      - "Workflow 1"
      - "Workflow 2"
    types:
      - completed

jobs:
  produce-cicd-data:
    runs-on: ubuntu-latest
    env:
        GH_TOKEN: ${{ github.token }}
    steps:
      - name: Collect CI/CD data
        uses: vmilosevic/actions/.github/actions/collect_data@main
        with:
          repository: ${{ github.repository }}
          run_id: ${{ github.event.workflow_run.id }}
          run_attempt: ${{ github.event.workflow_run.run_attempt }}
          sftp_host: <SFTP server hostname>
          sftp_user: <SFTP server username>
          ssh-private-key: <SSH private key>
```

Note: If `sftp_host` is empty, action will skip SFTP upload and it can be used for testing this way.

## Optional inputs

| Input | Default | Description |
|-------|---------|-------------|
| `skip_error_log_parsing` | `'false'` | Set to `'true'` to skip the per-line log scan that produces `failure_signature` and `failure_description` for failed jobs. Useful when a project's logs legitimately contain many "error"-like lines (e.g. tt-shield), where the scan produces misleading `failure_description` payloads and spams the collection step's stdout. Logs are still fetched — only the per-line scan and its `logger.info` emissions are skipped. |

Example usage with the flag enabled:

```yaml
- name: Collect CI/CD data
  uses: tenstorrent/tt-github-actions/.github/actions/collect_data@main
  with:
    repository: ${{ github.repository }}
    run_id: ${{ github.event.workflow_run.id }}
    run_attempt: ${{ github.event.workflow_run.run_attempt }}
    skip_error_log_parsing: 'true'
```

# Generate Data Analytics File

Scripts are used to automatically generate data for the analytics platform.
The workflow `produce_data.yml` is triggered upon the completion of other workflows and sends analytic data for that workflow.

Steps:
- Download the workflow and job information using the GitHub API
- Download workflow artifacts
- Download job logs
- Call `generate_data.py` to create a report file in JSON format
- Upload the file to the data analytics SFTP server

To run this manually, execute the following commands from the root folder:
```
download_workflow_data.sh tenstorrent/tt-forge-onnx 11236784732 1
GITHUB_EVENT_NAME=test python src/generate_data.py --run_id 11236784732
```
Where 11236784732 is the run_id of workflow we are generating data for

## Running tests

To run script tests install python dependancies and run pytest.

```
pytest --junitxml=pytest.xml --cov-report=term-missing --cov=src
```

## Running jobs manually

You can run the scripts locally, first to download the necesary files, and seccond one to generate report
```
download_workflow_data.sh tenstorrent/tt-forge-onnx 11253719387 1
GITHUB_EVENT_NAME=test python3 src/generate_data.py --run_id 11253719387
```

## Manually trigger data workflow

You can also trigger data workflow manually for some commit to test
```
gh workflow run "[internal] Produce analytic data" --ref vmilosevic/data_collection -f test_workflow_run_id=11253719387
```
