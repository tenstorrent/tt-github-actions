### Update Project date on issue update
A event automation that updates the tt-forge project date with the last issue date.

### Testing
##### Manually test has to be done since it diffcult to simulate a GH project at this time.

Create a branch/fork of this repo and a branch of your desired test repo ex. `tt-milr`

On the `issue-last-updated.yml`, update the branch reference to your the `tt-github-actions`
```bash
ex. uses: jmcgrathTT/tt-github-actions/.github/actions/${action_name}@${your_tt_github_actions_branch_name}
uses: jmcgrathTT/tt-github-actions/.github/actions/issue_add_last_updated@add-last-updated-workflow
```


In your test repo, create a test issue and your test repo's workflow
```bash
TEST_REPO='tenstorrent/tt-mlir'
TEST_REPO_BRANCH='2165-point-last-updated-workflow-to-tt-github-actions'

ISSUE_NUMBER=$(gh issue create -R $TEST_REPO --title 'Test issue last updated' --body 'Test issue last updated' | grep -oP '(?<=issues\/)\d+')

gh workflow run issue-last-updated.yml  -R $TEST_REPO --ref $TEST_REPO_BRANCH -f issue_number=$ISSUE_NUMBER
```

After you confirmed the workflow passses (in this case `tt-mlir`) you can delete your test issue.

```bash
gh issue delete -R tenstorrent/tt-mlir $ISSUE_NUMBER
```

Be sure to test show your test results in your PR!
