# SPDX License Checker Action

This GitHub Action checks SPDX license headers in source files using the [check-copyright](https://github.com/espressif/check-copyright) tool from Espressif.

## Features

- Validates SPDX license headers in source code files
- Supports multiple license types (Apache-2.0, MIT, BSD variants, etc.)
- Configurable via YAML configuration file
- Can run in dry-run mode for reporting without failing
- Supports custom file patterns and ignore lists

## Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `config_file` | Path to the check_copyright_config.yaml file relative to repository root | No | `check_copyright_config.yaml` |
| `target_directory` | Directory to check for SPDX headers | No | `.` (repository root) |
| `python_version` | Python version to use | No | `3.10` |
| `verbose` | Enable verbose output | No | `true` |
| `dry_run` | Run in dry-run mode (report issues without failing) | No | `false` |

## Usage

### Basic Usage

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

### With Custom Configuration

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
          config_file: '.github/check_copyright_config.yaml'
          verbose: 'true'
          dry_run: 'false'
```

## Configuration File

The action uses a YAML configuration file to specify which licenses are allowed and which files to check or ignore. A default configuration is provided in this directory as a template.

### Example Configuration

```yaml
DEFAULT:
  perform_check: yes
  
  allowed_licenses:
    - Apache-2.0
    - Apache-2.0 WITH LLVM-exception
    - MIT
    - BSD-2-Clause
    - BSD-3-Clause
  
  license_for_new_files: Apache-2.0
  
  new_notice_c: |
    // SPDX-FileCopyrightText: © {years} Tenstorrent AI ULC
    //
    // SPDX-License-Identifier: {license}
  
  new_notice_python: |
    """
    SPDX-FileCopyrightText: © {years} Tenstorrent AI ULC

    SPDX-License-Identifier: {license}
    """

ignore:
  perform_check: no
  include:
    - .github/
    - third_party/
    - __pycache__
    - build/
```

### Configuration Options

- **allowed_licenses**: List of SPDX license identifiers that are permitted
- **license_for_new_files**: Default license to use when adding new files
- **new_notice_c**: Template for C/C++/Header file copyright notices
- **new_notice_python**: Template for Python file copyright notices
- **ignore**: Patterns for files/directories to skip

## Origin

This action was extracted from the [tt-metal](https://github.com/tenstorrent/tt-metal) repository to standardize SPDX license checking across Tenstorrent repositories. See [PR #32654](https://github.com/tenstorrent/tt-metal/pull/32654) for the configuration updates that allowed third-party licenses.

## References

- [SPDX License List](https://spdx.org/licenses/)
- [check-copyright tool](https://github.com/espressif/check-copyright)
- [SPDX Specification](https://spdx.dev/specifications/)
