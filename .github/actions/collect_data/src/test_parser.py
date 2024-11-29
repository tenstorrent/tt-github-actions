# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
from loguru import logger
from typing import List
from pydantic_models import Test

from parsers.python_unittest_parser import PythonUnittestParser
from parsers.python_pytest_parser import PythonPytestParser

parsers = [
    PythonPytestParser(),
    PythonUnittestParser(),
]


def parse_file(filepath: str) -> List[Test]:
    """
    Parse a file using the appropriate parser.

    :param filepath: Path to the file to parse.
    :return: List of tests.
    """
    filepath = str(filepath)
    for parser in parsers:
        if parser.can_parse(filepath):
            try:
                return parser.parse(filepath)
            except Exception as e:
                logger.error(
                    f"Error parsing file: {filepath} using parser: {type(parser).__name__}, trying next parser."
                )
    logger.error(f"No parser available for file: {filepath}")
    return []
