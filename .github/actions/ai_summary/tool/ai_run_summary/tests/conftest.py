# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


@pytest.fixture
def all_fixture_files():
    return sorted(FIXTURES_DIR.glob("*.json"))


@pytest.fixture
def sample_summaries_dir(tmp_path):
    """Create a temp dir with all fixture JSON files, as parse_summaries_dir expects."""
    for f in FIXTURES_DIR.glob("*.json"):
        (tmp_path / f.name).write_text(f.read_text())
    return tmp_path
