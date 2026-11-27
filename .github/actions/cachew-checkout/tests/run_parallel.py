# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Run this directory's tests in parallel processes, using only the standard library.

Every test id is dealt round-robin into one shard per process, so a slow test
class is spread across all of them, and each shard runs as its own
`python -m unittest` process. Shards whose tests fail print their whole output,
then a combined summary. The tests use their own temporary directories and
servers on free ports, so they run side by side safely.

usage: python3 run_parallel.py [-j JOBS] [tests directory]
Exit status 1 if any test fails or errors, succeeds unexpectedly, a shard does
not run all its tests, a shard exits with a non-zero status, or the directory
holds no test.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest


# The counts unittest's summary line can carry, in its order.
COUNTS = ("failures", "errors", "skipped", "expected failures", "unexpected successes")


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        elif isinstance(item, unittest.loader._FailedTest):
            raise SystemExit(f"cannot import {item.id()}: run it alone to see the error")
        else:
            yield item.id()


def job_count(value):
    jobs = int(value)
    if jobs < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return jobs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-j", "--jobs", type=job_count, default=4, help="parallel processes (default: 4)")
    parser.add_argument("directory", nargs="?", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    sys.path.insert(0, str(directory))
    tests = list(test_ids(unittest.defaultTestLoader.discover(str(directory), top_level_dir=str(directory))))
    shards = [shard for shard in (tests[index :: args.jobs] for index in range(args.jobs)) if shard]
    start = time.monotonic()
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "unittest", *shard],
            cwd=directory,
            env=dict(os.environ, PYTHONUNBUFFERED="1"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for shard in shards
    ]
    totals = dict.fromkeys(("ran", *COUNTS), 0)
    complete = True
    exited_nonzero = 0
    for shard, process in zip(shards, processes):
        output = process.communicate()[0]
        # unittest's own summary ends the output; a test may print lines like it.
        summary = output.rstrip().rsplit("\n", 3)[-3:]
        ran = re.fullmatch(r"Ran (\d+) tests? in \S+", summary[0]) if len(summary) == 3 else None
        status = re.fullmatch(r"(?:OK|FAILED)(?: \((.*)\))?", summary[-1]) if ran else None
        if not status:
            ran = None
        totals["ran"] += int(ran[1]) if ran else 0
        # Each item whole: `expected failures=1` is not `failures=1`.
        for item in (status[1] or "").split(", ") if status else ():
            key, _, value = item.partition("=")
            if key in COUNTS and value.isdigit():
                totals[key] += int(value)
        if not ran or int(ran[1]) != len(shard):
            complete = False
        # A shard can print a passing summary and still exit non-zero (an
        # unexpected success, or a crash while the interpreter shuts down).
        if process.returncode:
            exited_nonzero += 1
            print(output)
            print(f"shard exited with status {process.returncode}")
    # No test at all is a wrong directory, not a pass.
    complete = complete and totals["ran"] == len(tests) > 0
    failed = totals["failures"] or totals["errors"] or totals["unexpected successes"] or not complete or exited_nonzero
    print(
        f"Ran {totals['ran']} of {len(tests)} tests on {len(processes)} processes in "
        f"{time.monotonic() - start:.0f}s: {'FAILED' if failed else 'OK'} "
        f"({', '.join(f'{key}={totals[key]}' for key in COUNTS)}, shards exited non-zero={exited_nonzero})"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
