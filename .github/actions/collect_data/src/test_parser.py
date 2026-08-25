# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
from dataclasses import dataclass, field
from loguru import logger
from typing import List, Optional, Union
from pydantic_models import Test, OpTest

from parsers.python_unittest_parser import PythonUnittestParser
from parsers.python_pytest_parser import PythonPytestParser
from parsers.parameter_support_test_parser import ParameterSupportTestParser

parsers = [
    ParameterSupportTestParser(),
    PythonPytestParser(),
    PythonUnittestParser(),
]


@dataclass
class ParseResult:
    """Everything extracted from a single report file."""

    tests: List[Union[Test, OpTest]] = field(default_factory=list)
    job_tags: Optional[dict] = None


def parse_file(
    filepath: str,
    project: Optional[str] = None,
    github_job_id: Optional[int] = None,
) -> ParseResult:
    """
    Parse a file using the appropriate parser.

    :param filepath: Path to the file to parse.
    :return: ParseResult with the tests and job-level tags found in the file.
    """
    filepath = str(filepath)
    for parser in parsers:
        if parser.can_parse(filepath):
            try:
                tests = parser.parse(filepath, project=project, github_job_id=github_job_id)
            except Exception as e:
                logger.error(f"Error parsing file: {filepath} using parser: {type(parser).__name__}")
                logger.error(f"Exception: {e}")
                logger.error("Trying next parser")
                continue
            try:
                job_tags = parser.get_job_tags(filepath)
            except Exception as e:
                logger.warning(f"Error extracting job tags from {filepath}: {e}")
                job_tags = None
            return ParseResult(tests=tests, job_tags=job_tags)
    logger.error(f"No parser available for file: {filepath}")
    return ParseResult()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python test_parser.py <file>")
        sys.exit(1)
    print(parse_file(sys.argv[1]))
