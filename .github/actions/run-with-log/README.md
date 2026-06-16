# `run-with-log`

Run a bash script like a normal `run:` step, but capture its combined output to
a `.log` file and append a completion marker as the final line:

```
[==log-finish-line==] exit_code=<N>
```

The marker is a generic end-of-run sentinel. Its **absence** means the shell was
killed before finishing — most usefully a GitHub `timeout-minutes` kill, which
leaves no trace in the log otherwise. Tooling that reads the logs (e.g. the
`ai_summary` parser) uses this to tell a timeout apart from a crash.

Replaces hand-rolled `tee` / `PIPESTATUS` blocks copied across workflows.

## Usage

```yaml
- name: ${{ matrix.test-group.name }}
  timeout-minutes: ${{ matrix.test-group.timeout }}
  uses: tenstorrent/tt-github-actions/.github/actions/run-with-log@main
  with:
    log-file: generated/test_logs/${{ matrix.test-group.name }}.log
    run: |
      pytest models/demos/... -xv
      ./some_other_command
```

## Inputs

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `run` | yes | — | Bash script, exactly as a `run:` step body. |
| `log-file` | yes | — | Path for the `.log`, relative to `working-directory`. Parent dirs are created. |
| `working-directory` | no | `/work` | Defaults to the Tenstorrent container workdir; override per call as on a `run:` step. Needed because composite steps don't inherit the job's `defaults.run.working-directory`. |

## What propagates

- **`if:`, `env:`, `timeout-minutes`** on the calling step — native; `timeout-minutes`
  is what makes the marker meaningful (the kill skips it).
- **Exit code / pass-fail** — the script's real exit code is forwarded, so the
  step fails when the script fails and `failure()` / `if:` downstream work.
- **The script itself** runs under `bash -eo pipefail` (same as a `run:` step).

**Does not propagate:** named `$GITHUB_OUTPUT` written inside the script — a
composite action can only expose declared outputs, not arbitrary ones. Steps
that must set outputs should stay plain `run:` steps.
