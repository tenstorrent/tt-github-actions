# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

report_failure = False


def failure_happened():
    report_failure = True


def is_failure():
    return report_failure

def is_valid_testcase_(testcase) -> bool:
    """
    Some cases of invalid tests include:

    - GitHub times out pytest so it records something like this:
        </testcase>
        <testcase time="0.032"/>
    """
    if "name" not in testcase.attrib or "classname" not in testcase.attrib:
        # This should be able to capture all cases where there's no info
        logger.warning("Found invalid test case with: no name nor classname")
        return False
    else:
        return True
