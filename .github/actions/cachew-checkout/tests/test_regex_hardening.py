# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The action's regular expressions under hostile input.

Python's re backtracks, so a pattern that can match the same text more than one
way (`(?:\\s+)*`, say) can take exponential time on input that almost matches.
Workflow files, Git output and action inputs all reach these patterns, so each
compiled pattern must stay fast on long runs of the characters it consumes.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ACTION = Path(__file__).resolve().parents[1]
MODULES = ("checkout", "migrate", "session")

# Runs every compiled pattern of the modules against repeated pieces followed by
# a character that makes the match fail, and prints the slowest calls.
PROBE = textwrap.dedent(
    r"""
    import importlib.util, itertools, json, re, sys, time
    from pathlib import Path

    action = Path(sys.argv[1])
    pieces = [" ", "\t", " \t", "\n", " \n", "\r\n", "- ", "-", " - ", "!", "!a ", "&a ", "!a", "&a", "! ", "#", " #",
              "a", "a:", ": ", ":", "'", '"', "''", "\\", "\\x", "\\\n", "/", "./", "a/", "@", "a@", "tenstorrent/",
              "{", "[", ",", "0", "1.", "v1.", "v1 ", "cachew checkout ", " ;", "a-", "-a", ".", "[::", "uses", "?"]
    tails = ["", "\x00", "x", "!", "\n", "{", "[", "#", ":", "'", '"', "@"]
    slow = []
    for name in sys.argv[2:]:
        spec = importlib.util.spec_from_file_location(name, action / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attr, pattern in sorted(vars(module).items()):
            if not isinstance(pattern, re.Pattern):
                continue
            for piece, tail in itertools.product(pieces, tails):
                text = piece * 48 + tail
                if isinstance(pattern.pattern, bytes):
                    text = text.encode()
                for method in (pattern.match, pattern.fullmatch, pattern.search):
                    start = time.perf_counter()
                    method(text)
                    elapsed = time.perf_counter() - start
                    if elapsed > 0.05:
                        slow.append([f"{name}.{attr}", method.__name__, piece, tail, round(elapsed, 3)])
                        break
    print(json.dumps(slow))
    """
)


class BacktrackingTests(unittest.TestCase):
    def test_every_pattern_stays_fast_on_near_misses(self):
        # In a child process with a deadline: a pattern that backtracks
        # exponentially does not return, and re cannot be interrupted.
        try:
            result = subprocess.run(
                [sys.executable, "-c", PROBE, str(ACTION), *MODULES], capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            self.fail("a pattern did not finish on a near miss of 48 repeated pieces (exponential backtracking)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_a_steps_key_followed_by_blank_lines_with_spaces_is_read_quickly(self):
        # The form that hung --check: indented blank lines after steps:.
        text = "jobs:\n  build:\n    runs-on: self-hosted\n    steps:" + "      \n" * 12
        text += "      - uses: actions/checkout@v7.0.1\n"
        text += "  other: " + "   \n" * 12 + "    {runs-on: self-hosted}\n"
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('migrate', {str(ACTION / 'migrate.py')!r})\n"
            "migrate = importlib.util.module_from_spec(spec); spec.loader.exec_module(migrate)\n"
            "text = sys.stdin.read()\n"
            "print(migrate.hidden_checkouts(migrate.rewrite(text, 'a' * 40)), migrate.flow_steps(text))\n"
        )
        try:
            result = subprocess.run([sys.executable, "-c", script], input=text, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("migrate.py did not finish on blank lines with trailing spaces after steps:")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[] []")


if __name__ == "__main__":
    unittest.main()
