#!/usr/bin/env python3
# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Merge default SPDX configuration with user-provided ignore patterns.

This script creates a complete configuration by combining the standard
company-wide license settings with repository-specific ignore patterns.
"""

import sys
import yaml
from pathlib import Path
from copy import deepcopy

# Default configuration with company-wide standards
DEFAULT_CONFIG = {
    "DEFAULT": {
        "perform_check": True,
        "allowed_licenses": ["Apache-2.0", "Apache-2.0 WITH LLVM-exception", "MIT", "BSD-2-Clause", "BSD-3-Clause"],
        "license_for_new_files": "Apache-2.0",
        "new_notice_c": (
            "// SPDX-FileCopyrightText: © {years} Tenstorrent USA, Inc.\n"
            "//\n"
            "// SPDX-License-Identifier: {license}\n"
        ),
        "new_notice_python": (
            '"""\n'
            "SPDX-FileCopyrightText: © {years} Tenstorrent USA, Inc.\n"
            "\n"
            "SPDX-License-Identifier: {license}\n"
            '"""\n'
        ),
    },
    "ignore": {"perform_check": False, "include": []},
}


def merge_configs(user_config_path=None):
    """Merge default config with user's ignore patterns."""
    config = deepcopy(DEFAULT_CONFIG)

    if user_config_path and Path(user_config_path).exists():
        with open(user_config_path, "r") as f:
            user_config = yaml.safe_load(f) or {}

        # Only allow users to specify ignore patterns
        if "ignore" in user_config:
            ignore_section = user_config["ignore"]
            if isinstance(ignore_section, dict):
                if "include" in ignore_section:
                    config["ignore"]["include"] = ignore_section["include"]
                if "perform_check" in ignore_section:
                    config["ignore"]["perform_check"] = ignore_section["perform_check"]

    return config


def main():
    if len(sys.argv) > 1:
        user_config = sys.argv[1]
    else:
        user_config = None

    output_file = sys.argv[2] if len(sys.argv) > 2 else "merged_config.yaml"

    merged = merge_configs(user_config)

    with open(output_file, "w") as f:
        yaml.dump(merged, f, default_flow_style=False, sort_keys=False)

    print(f"Merged configuration written to {output_file}")


if __name__ == "__main__":
    main()
