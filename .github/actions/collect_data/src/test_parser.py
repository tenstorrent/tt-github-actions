# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
from loguru import logger
from typing import List, Optional, Union
from pydantic_models import Test, OpTest

from parsers.python_unittest_parser import PythonUnittestParser
from parsers.python_pytest_parser import PythonPytestParser

parsers = [
    PythonPytestParser(),
    PythonUnittestParser(),
]


def parse_file(
    filepath: str,
    project: Optional[str] = None,
    github_job_id: Optional[int] = None,
    job_name: Optional[str] = None,
    git_branch: Optional[str] = None,
) -> List[Union[Test, OpTest]]:
    """
    Parse a file using the appropriate parser.

    :param filepath: Path to the file to parse.
    :param project: Project name.
    :param github_job_id: GitHub job ID.
    :param job_name: Job name for parser selection.
    :param git_branch: Git branch name for filtering builder parser usage.
    :return: List of tests.
    """
    filepath = str(filepath)

    # Use default parsers for other jobs
    for parser in parsers:
        if parser.can_parse(filepath):
            try:
                return parser.parse(filepath, project=project, github_job_id=github_job_id)
            except Exception as e:
                logger.error(f"Error parsing file: {filepath} using parser: {type(parser).__name__}")
                logger.error(f"Exception: {e}")
                logger.error("Trying next parser")
    logger.error(f"No parser available for file: {filepath}")
    return []


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python test_parser.py <file>")
        sys.exit(1)
    print(parse_file(sys.argv[1]))
