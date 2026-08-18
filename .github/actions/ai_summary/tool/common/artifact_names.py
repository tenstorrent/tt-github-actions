# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Output naming shared by the job and run stages.

``ai_summary/run`` flattens every leg's artifacts into one directory, so the
producer's names and the reader's parser have to agree. Keeping both here stops
them drifting apart.
"""

from __future__ import annotations

import re

__all__ = ["job_id_from_stem", "qualified_stem"]

_JOB_SEGMENT = re.compile(r"_j(\d+)$")


def qualified_stem(prefix: str, run_id: str = "", attempt: int | None = None, job_id: str = "") -> str:
    """``<prefix>_r<run_id>_a<attempt>_j<job_id>``, omitting segments not supplied.

    Segments are absent outside CI, where there is no run or attempt.
    """
    parts = [prefix]
    if run_id:
        parts.append(f"r{run_id}")
    if attempt:
        parts.append(f"a{attempt}")
    if job_id:
        parts.append(f"j{job_id}")
    return "_".join(parts)


def job_id_from_stem(stem: str) -> str:
    """The job id in ``stem``, or "" when it carries no ``_j<digits>`` segment.

    Matched rather than split off the tail: a stem without a job id ends in the
    attempt, and returning that would read as a job id.
    """
    m = _JOB_SEGMENT.search(stem)
    return m.group(1) if m else ""
