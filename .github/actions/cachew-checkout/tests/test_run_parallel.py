# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parent / "run_parallel.py"
PASSING = "import unittest\n\n\nclass T(unittest.TestCase):\n" + "".join(
    f"    def test_{name}(self):\n        pass\n\n" for name in "abcde"
)


class RunParallelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def run_tests(self, *options):
        return subprocess.run(
            [sys.executable, str(RUNNER), *options, str(self.directory)], capture_output=True, text=True, timeout=120
        )

    def test_every_test_runs_and_a_failure_fails_the_run(self):
        (self.directory / "test_sample.py").write_text(PASSING)
        result = self.run_tests("-j", "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 5 of 5 tests on 2 processes", result.stdout)
        (self.directory / "test_sample.py").write_text(PASSING.replace("pass", "self.fail('no')", 1))
        result = self.run_tests("-j", "2")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED (failures=1", result.stdout)

    def test_a_job_count_below_one_is_refused(self):
        # A negative count once ran the first test alone and reported OK.
        (self.directory / "test_sample.py").write_text(PASSING)
        for jobs in ("-1", "0"):
            with self.subTest(jobs=jobs):
                result = self.run_tests(f"--jobs={jobs}")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("OK", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_a_directory_without_tests_fails(self):
        result = self.run_tests()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("OK", result.stdout)

    def test_a_summary_line_printed_by_a_test_is_not_the_shards_count(self):
        (self.directory / "test_sample.py").write_text(
            "import sys\nimport unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_a(self):\n        print('Ran 1 test in 0.001s\\n\\nFAILED (failures=7)', file=sys.stderr)\n"
        )
        result = self.run_tests("-j", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK (failures=0", result.stdout)


    def test_an_expected_failure_is_not_counted_as_a_failure(self):
        # unittest reports it as `OK (expected failures=1)`.
        (self.directory / "test_sample.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n    def test_a(self):\n        self.fail('expected')\n\n"
            "    def test_b(self):\n        pass\n"
        )
        result = self.run_tests("-j", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK (failures=0, errors=0, skipped=0, expected failures=1, unexpected successes=0", result.stdout)

    def test_an_unexpected_success_fails_the_run(self):
        (self.directory / "test_sample.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n    def test_a(self):\n        pass\n"
        )
        result = self.run_tests("-j", "1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED (failures=0, errors=0, skipped=0, expected failures=0, unexpected successes=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
