# SPDX-FileCopyrightText: (c) 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_ambient_ci_env(monkeypatch):
    """Clear the GitHub env every test starts from.

    Output names and run metadata are derived from these at the point of use, so
    an ambient value makes a test assert one thing locally and another in CI.
    Tests that need them set them explicitly.
    """
    for var in (
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_REPOSITORY",
        "GITHUB_SERVER_URL",
        "GITHUB_REF",
        "GITHUB_EVENT_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


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
