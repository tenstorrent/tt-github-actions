# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Union

from pydantic_models import Test, OpTest


class ParserError(Exception):
    """Custom exception for parser errors."""

    pass


@dataclass
class ParseResult:
    """Everything extracted from a single report file."""

    tests: List[Union[Test, OpTest]] = field(default_factory=list)
    job_tags: Optional[dict] = None


class Parser(ABC):
    """Abstract base class for parsers."""

    @abstractmethod
    def can_parse(self, filepath: str) -> bool:
        """
        Check if the parser can parse the file.
        :param filepath: Path to the file to check.
        :return: True if the parser can parse the file, False otherwise.
        """
        pass

    @abstractmethod
    def parse(
        self,
        filepath: str,
        project: Optional[str] = None,
        github_job_id: Optional[int] = None,
    ) -> ParseResult:
        """
        Parse a file and return the tests and job-level tags found in it.
        :param filepath: Path to the file to parse.
        :return: ParseResult with the tests and job-level tags.
        :raises ParserError: If parsing fails.
        """
        pass
