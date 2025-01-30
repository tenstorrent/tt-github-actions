# Job id action

This action provides the way to gete current job id.
Github jobs dont have job_id available in context but we can use Github API to fetch it.
Fetching is done based on pipeline run_id and job name, which must be exact, take special care in case of matrix runs, where job name is auto generated.


## Basic usage

```
- name: Fetch job id
  id: fetch-job-id
  uses: tenstorrent/tt-github-actions/.github/actions/job_id
  with:
    job_name: "${{ github.job }} (${{ matrix.test_group_id }})"
- name: Debug print
  run: |
    echo github.job "${{ github.job }}" pipeline "${{ github.run_id }}" attempt "${{ github.run_attempt }}"
    echo JOB_ID: "${{ steps.fetch-job-id.outputs.job_id }}"
```
