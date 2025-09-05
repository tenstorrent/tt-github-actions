# License Header Checker

A Python tool for checking and managing SPDX license headers in source code files.

## Overview

This tool scans source code files to verify that they contain the proper SPDX license headers. It can also add or fix headers in files that are missing or have incorrect headers.

## Features

- Checks files for proper SPDX license headers
- Supports multiple file types (Python, C/C++, Shell scripts, JavaScript, TypeScript, Java, Go)
- Can add missing license headers
- Can fix incorrect license headers
- Automatically determines file creation year from git history

## Usage

```bash
# Check all files in a directory
python check_license_headers.py --path /path/to/check

# Fix missing or incorrect headers
python check_license_headers.py --path /path/to/check --fix

# Show only errors (not successful files)
python check_license_headers.py --path /path/to/check --only-errors

# Check files modified in the last commit
python check_license_headers.py --modified
```

## Testing

The tool comes with a comprehensive test suite. To run the tests:

```bash
cd .github/scripts/check_license
python -m unittest tests/test_check_license_headers.py

# Or with pytest for coverage reports
pytest --cov=. tests/
```

## License

SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC

SPDX-License-Identifier: Apache-2.0
