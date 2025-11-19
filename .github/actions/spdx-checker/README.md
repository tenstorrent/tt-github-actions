# SPDX License Checker Action

This GitHub Action checks SPDX license headers in source files using the [check-copyright](https://github.com/espressif/check-copyright) tool from Espressif.

## Features

- Validates SPDX license headers in source code files
- Enforces standardized license settings across all Tenstorrent repositories
- Supports multiple license types (Apache-2.0, MIT, BSD variants, etc.)
- Built-in default configuration with company-wide standards
- Optional repository-specific ignore patterns
- Can run in dry-run mode for reporting without failing

## Standard License Configuration

The action uses a **built-in default configuration** that enforces company-wide standards:

- **Allowed Licenses**: Apache-2.0, Apache-2.0 WITH LLVM-exception, MIT, BSD-2-Clause, BSD-3-Clause
- **Default License for New Files**: Apache-2.0
- **Copyright Holder**: Tenstorrent AI ULC

These settings **cannot be overridden** to ensure consistency across all repositories. Repositories can only specify which files/directories to ignore.

## Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `ignore_config_file` | Path to YAML file containing only "ignore" patterns. Leave empty to use minimal defaults. | No | `` (empty) |
| `target_directory` | Directory to check for SPDX headers | No | `.` (repository root) |
| `python_version` | Python version to use | No | `3.10` |
| `verbose` | Enable verbose output | No | `true` |
| `dry_run` | Run in dry-run mode (report issues without failing) | No | `false` |

## Usage

### Basic Usage (No Custom Ignores)

For new repositories that don't need custom ignore patterns:

```yaml
name: Check SPDX Licenses

on: [pull_request]

jobs:
  check-spdx:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Check SPDX licenses
        uses: tenstorrent/tt-github-actions/.github/actions/spdx-checker@main
```

### With Custom Ignore Patterns

For repositories that need to ignore specific files/directories:

```yaml
name: Check SPDX Licenses

on: [pull_request]

jobs:
  check-spdx:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Check SPDX licenses
        uses: tenstorrent/tt-github-actions/.github/actions/spdx-checker@main
        with:
          ignore_config_file: '.github/spdx_ignore.yaml'
          verbose: 'true'
```

## Ignore Configuration File

If your repository needs to ignore specific files or directories, create a simple YAML file containing **only** an `ignore` section. The action will automatically merge this with the standard license configuration.

### Example Ignore Configuration

Create a file (e.g., `.github/spdx_ignore.yaml`) in your repository:

```yaml
ignore:
  perform_check: no
  include:
    # Version-controlled but should be ignored
    - third_party/
    - vendor/
    - external/
    - generated/
    
    # Build artifacts
    - build/
    - dist/
    - __pycache__
    
    # File types that don't need SPDX headers
    - "*.ld"
    - "*.S"
    - "*.json"
    - "*.md"
```

### Important Notes

- **Only the `ignore` section is respected** - any other configuration options will be ignored
- License settings are standardized and cannot be changed per-repository
- An example template is provided in this action's directory: `check_copyright_config.yaml`

## Origin

This action was extracted from the [tt-metal](https://github.com/tenstorrent/tt-metal) repository to standardize SPDX license checking across Tenstorrent repositories. See [PR #32654](https://github.com/tenstorrent/tt-metal/pull/32654) for the configuration updates that allowed third-party licenses.

## References

- [SPDX License List](https://spdx.org/licenses/)
- [check-copyright tool](https://github.com/espressif/check-copyright)
- [SPDX Specification](https://spdx.dev/specifications/)
