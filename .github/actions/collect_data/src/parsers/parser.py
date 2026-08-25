# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod
from typing import Optional


class ParserError(Exception):
    """Custom exception for parser errors."""

    pass


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
    ) -> list:
        """
        Parse a file and return a list of tests.
        :param filepath: Path to the file to parse.
        :return: List of tests.
        :raises ParserError: If parsing fails.
        """
        pass

    def get_job_tags(self, filepath: str) -> Optional[dict]:
        """
        Extract job-level tags from a report file, if the format supports them.
        :param filepath: Path to the file to extract tags from.
        :return: Tags as a dict, or None if the report carries no job-level tags.
        """
        return None
