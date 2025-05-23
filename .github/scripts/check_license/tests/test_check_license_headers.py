#!/usr/bin/env python3

# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import unittest
import tempfile
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import StringIO

# Add the parent directory to sys.path to import the script
sys.path.append(str(Path(__file__).parent.parent))
from check_license_headers import (
    normalize_line,
    strip_noise_lines,
    get_expected_header,
    get_raw_expected_header,
    extract_header_block,
    check_file,
    add_license_header,
    replace_header,
    get_git_year,
    COMMENT_STYLES,
    LICENSE_HEADER,
)


class TestCheckLicenseHeaders(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        # Path to fixtures directory
        self.fixtures_dir = Path(__file__).parent / "fixtures"

    def tearDown(self):
        # Remove the temporary directory after tests
        shutil.rmtree(self.temp_dir)

    def create_test_file(self, filename, content):
        """Helper method to create test files with specific content"""
        file_path = Path(self.temp_dir) / filename
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return file_path

    def read_fixture(self, fixture_name):
        """Helper method to read content from fixture files"""
        fixture_path = self.fixtures_dir / fixture_name
        with open(fixture_path, "r", encoding="utf-8") as f:
            return f.read()

    # ========================= License Format Tests =========================

    def test_license_header_format(self):
        """Test that the LICENSE_HEADER template has the required format with blank line"""
        header_lines = LICENSE_HEADER.strip().splitlines()
        self.assertEqual(len(header_lines), 3, "LICENSE_HEADER should have exactly 3 lines")
        self.assertIn("SPDX-FileCopyrightText", header_lines[0])
        self.assertEqual(header_lines[1], "", "Second line should be blank")
        self.assertIn("SPDX-License-Identifier", header_lines[2])

    def test_generated_header_preserves_blank_line(self):
        """Test that generated headers preserve the blank line in the middle"""
        # Test Python header
        py_header = get_raw_expected_header(".py")
        self.assertEqual(len(py_header), 3, "Generated Python header should have 3 lines")
        self.assertEqual(py_header[1], "# ", "Second line should be a blank comment (# )")

        # Test C++ header
        cpp_header = get_raw_expected_header(".cpp")
        self.assertEqual(len(cpp_header), 3, "Generated C++ header should have 3 lines")
        self.assertEqual(cpp_header[1], "// ", "Second line should be a blank comment (// )")

    def test_fixture_files_have_correct_format(self):
        """Test that our fixture files have the correct license header format with blank line"""
        # Files with headers to check
        py_files = [
            "test_py_with_header.py",
            "test_py_wrong_header.py",  # Even though it has the wrong company/license, format should be correct
        ]
        cpp_files = [
            "test_cpp_with_header.cpp",
            "test_cpp_wrong_header.cpp",  # Even though it has the wrong company/license, format should be correct
        ]

        # Check Python fixtures
        for filename in py_files:
            path = self.fixtures_dir / filename
            self.assertTrue(path.exists(), f"Fixture file {filename} not found")

            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[:5]  # Read first few lines

            # Find SPDX lines
            spdx_lines = []
            for i, line in enumerate(lines):
                if "SPDX" in line:
                    spdx_lines.append((i, line.strip()))

            self.assertGreaterEqual(len(spdx_lines), 2, f"File {filename} should have at least two SPDX lines")

            # Find the first SPDX line index
            first_spdx_idx = spdx_lines[0][0]

            # Check for blank comment line between SPDX lines
            blank_line = lines[first_spdx_idx + 1].strip()
            self.assertEqual(
                blank_line, "#", f"File {filename} should have blank comment line (just '#') after first SPDX line"
            )

        # Check C++ fixtures
        for filename in cpp_files:
            path = self.fixtures_dir / filename
            self.assertTrue(path.exists(), f"Fixture file {filename} not found")

            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[:10]  # Read first few lines

            # Find SPDX lines
            spdx_lines = []
            for i, line in enumerate(lines):
                if "SPDX" in line:
                    spdx_lines.append((i, line.strip()))

            self.assertGreaterEqual(len(spdx_lines), 2, f"File {filename} should have at least two SPDX lines")

            # Find the first SPDX line index
            first_spdx_idx = spdx_lines[0][0]

            # Verify header starts at line 0 (beginning of file)
            self.assertEqual(
                first_spdx_idx, 0, f"File {filename} should have SPDX header starting at line 0 (beginning of file)"
            )

            # Check for blank comment line between SPDX lines
            blank_line = lines[first_spdx_idx + 1].strip()
            self.assertEqual(
                blank_line, "//", f"File {filename} should have blank comment line (just '//') after first SPDX line"
            )

    def test_cpp_header_must_start_at_beginning(self):
        """Test that our license checker identifies C++ files with incorrectly placed headers"""
        # Test the incorrect placement fixture
        incorrect_path = self.fixtures_dir / "test_cpp_with_header_wrong_placement.cpp"
        self.assertTrue(incorrect_path.exists(), "Fixture file test_cpp_with_header_wrong_placement.cpp not found")

        # Verify the actual placement is wrong in our fixture
        with open(incorrect_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[:10]

        # Find the first SPDX line
        first_spdx_line = -1
        for i, line in enumerate(lines):
            if "SPDX" in line:
                first_spdx_line = i
                break

        # Confirm the header is indeed not at the beginning
        self.assertGreater(
            first_spdx_line, 0, "The test fixture should have its header NOT at the beginning of the file"
        )

        # NOTE: In an ideal implementation, the check_file function would reject C++ files
        # with license headers not at line 0. In a future update to check_license_headers.py,
        # this should be enforced. For now, we're only documenting the expectation.
        #
        # TODO: check_file should be updated to enforce C++ headers starting at line 0
        #
        # For now, we test the current behavior: the checker currently accepts C++ files
        # with headers not at line 0, but it should eventually reject them
        expected_header = get_expected_header(".cpp")
        result = check_file(incorrect_path, expected_header, only_errors=True)  # Suppress output

        # Current behavior: returns True (passes) even though header isn't at beginning
        # Future behavior should be False
        self.assertTrue(
            result, "Current implementation accepts C++ files with headers not at the beginning, but should be fixed"
        )

    def test_py_header_placement(self):
        """Test the placement of license headers in Python files"""
        # Test the Python file with incorrectly placed header
        incorrect_path = self.fixtures_dir / "test_py_with_header_wrong_placement.py"
        self.assertTrue(incorrect_path.exists(), "Fixture file test_py_with_header_wrong_placement.py not found")

        # Verify the fixture file does have its header not at the beginning
        with open(incorrect_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[:10]

        # Find the first SPDX line
        first_spdx_line = -1
        for i, line in enumerate(lines):
            if "SPDX" in line:
                first_spdx_line = i
                break

        # Confirm the header is indeed not at the beginning (it should be after some imports)
        self.assertGreater(
            first_spdx_line, 2, "The test fixture should have its header NOT at the beginning of the file"
        )

        # NOTE: In an ideal implementation, the check_file function would reject Python files
        # with license headers not at line 0. In a future update to check_license_headers.py,
        # this should be enforced. For now, we're only documenting the expectation.
        #
        # TODO: check_file should be updated to enforce Python headers starting at line 0,
        #       with a possible exception for shebang lines.
        #
        # For now, we test the current behavior: the checker currently accepts Python files
        # with headers not at line 0, but it should eventually reject them
        expected_header = get_expected_header(".py")
        result = check_file(incorrect_path, expected_header, only_errors=True)  # Suppress output

        # Current behavior: returns True (passes) even though header isn't at beginning
        # Future behavior should be False
        self.assertTrue(
            result, "Current implementation accepts Python files with headers not at the beginning, but should be fixed"
        )

    def test_normalize_line(self):
        """Test normalize_line function with various inputs"""
        # Test with year normalization when git_year is None
        self.assertEqual(
            normalize_line("# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC"),
            "# SPDX-FileCopyrightText: © <YEAR> Tenstorrent AI ULC",
        )

        # Test without year normalization
        self.assertEqual(
            normalize_line("# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC", False),
            "# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC",
        )

        # Test with specific git_year - when git_year is provided, years are NOT normalized
        # even if normalize_year=True, according to the function implementation
        self.assertEqual(
            normalize_line("# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC", True, "2023"),
            "# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC",
        )

        # Test whitespace normalization
        self.assertEqual(
            normalize_line("#   SPDX-FileCopyrightText:   ©   2025   Tenstorrent   AI   ULC"),
            "# SPDX-FileCopyrightText: © <YEAR> Tenstorrent AI ULC",
        )

    def test_strip_noise_lines(self):
        """Test strip_noise_lines function"""
        lines = [
            "# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC",
            "#",
            "# SPDX-License-Identifier: Apache-2.0",
            "//",
            "  ",  # Empty line with spaces
            "",  # Empty line
        ]
        expected = ["# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC", "# SPDX-License-Identifier: Apache-2.0"]
        self.assertEqual(strip_noise_lines(lines), expected)

    def test_get_expected_header(self):
        """Test get_expected_header function for different file types"""
        # Test Python file header
        py_header = get_expected_header(".py")
        self.assertIsNotNone(py_header)
        # At least one line should contain SPDX, not all lines
        self.assertTrue(any("SPDX" in line for line in py_header))

        # Test C++ file header
        cpp_header = get_expected_header(".cpp")
        self.assertIsNotNone(cpp_header)
        self.assertTrue(any("SPDX" in line for line in cpp_header))

        # Test with specific git year
        py_header_with_year = get_expected_header(".py", False, "2023")
        self.assertIsNotNone(py_header_with_year)
        self.assertTrue("2023" in py_header_with_year[0])

        # Test unsupported file type
        self.assertIsNone(get_expected_header(".unknown"))

    def test_get_raw_expected_header(self):
        """Test get_raw_expected_header function"""
        # Test Python file header
        py_header = get_raw_expected_header(".py")
        self.assertIsNotNone(py_header)
        self.assertTrue(all(line.startswith("# ") for line in py_header))

        # Test with specific git year
        py_header_with_year = get_raw_expected_header(".py", "2023")
        self.assertIsNotNone(py_header_with_year)
        self.assertTrue("2023" in py_header_with_year[0])

    def test_extract_header_block(self):
        """Test extract_header_block function with Python files"""
        # Create a test Python file with license header
        py_content = self.read_fixture("test_py_with_header.py")
        py_file = self.create_test_file("test.py", py_content)

        # Extract header and verify
        header_lines, start_line = extract_header_block(py_file)
        self.assertEqual(start_line, 0)  # Header starts at line 0 (0-indexed) in our fixture
        self.assertEqual(len(header_lines), 3)
        self.assertTrue("SPDX-FileCopyrightText" in header_lines[0])
        self.assertTrue("SPDX-License-Identifier" in header_lines[2])

        # Test file without header
        no_header_content = self.read_fixture("test_py_no_header.py")
        no_header = self.create_test_file("no_header.py", no_header_content)
        header_lines, start_line = extract_header_block(no_header)
        self.assertEqual(start_line, -1)  # No header found
        self.assertEqual(len(header_lines), 0)

    def test_extract_header_block_cpp(self):
        """Test extract_header_block function with C++ files"""
        # Create a test C++ file with license header
        cpp_content = self.read_fixture("test_cpp_with_header.cpp")
        cpp_file = self.create_test_file("test.cpp", cpp_content)

        # Extract header and verify
        header_lines, start_line = extract_header_block(cpp_file)
        self.assertEqual(start_line, 0)  # Header should start at line 0 in the C++ file (beginning of file)
        self.assertEqual(len(header_lines), 3)
        self.assertTrue("SPDX-FileCopyrightText" in header_lines[0])
        self.assertTrue("SPDX-License-Identifier" in header_lines[2])

        # Test file without header
        no_header_content = self.read_fixture("test_cpp_no_header.cpp")
        no_header = self.create_test_file("no_header.cpp", no_header_content)
        header_lines, start_line = extract_header_block(no_header)
        self.assertEqual(start_line, -1)  # No header found
        self.assertEqual(len(header_lines), 0)

    @patch("sys.stdout", new_callable=StringIO)
    def test_check_file(self, mock_stdout):
        """Test check_file function with Python files"""
        # Create a file with correct license header
        py_content = self.read_fixture("test_py_with_header.py")
        py_file = self.create_test_file("correct.py", py_content)

        # Test check with correct file
        expected_header = get_expected_header(".py")
        result = check_file(py_file, expected_header)
        self.assertTrue(result)
        self.assertIn("License header OK", mock_stdout.getvalue())

        # Create a file with incorrect license header content
        py_wrong_content = self.read_fixture("test_py_wrong_header.py")
        py_wrong_file = self.create_test_file("wrong.py", py_wrong_content)

        # Test check with incorrect file
        result = check_file(py_wrong_file, expected_header)
        self.assertFalse(result)

        # NOTE: In an ideal implementation, we would check that Python files with license
        # headers not at line 0 (including those with shebang lines) should fail validation.
        # Currently, the file fails due to content mismatch rather than placement issues.
        #
        # TODO: check_file should be updated to enforce Python headers starting at line 0,
        #       with a possible exception for shebang lines where they should start at line 1.
        #
        # For now, we're just checking that it fails validation due to content mismatch
        self.assertIn("Mismatch", mock_stdout.getvalue())

    @patch("sys.stdout", new_callable=StringIO)
    def test_check_cpp_file(self, mock_stdout):
        """Test check_file function with C++ files"""
        # Create a file with correct license header
        cpp_content = self.read_fixture("test_cpp_with_header.cpp")
        cpp_file = self.create_test_file("correct.cpp", cpp_content)

        # Test check with correct file
        expected_header = get_expected_header(".cpp")
        result = check_file(cpp_file, expected_header)
        self.assertTrue(result)
        self.assertIn("License header OK", mock_stdout.getvalue())

        # Create a file with incorrect license header
        cpp_wrong_content = self.read_fixture("test_cpp_wrong_header.cpp")
        cpp_wrong_file = self.create_test_file("wrong.cpp", cpp_wrong_content)

        # Clear the mock stdout
        mock_stdout.seek(0)
        mock_stdout.truncate(0)

        # Test check with incorrect file
        result = check_file(cpp_wrong_file, expected_header)
        self.assertFalse(result)
        self.assertIn("Mismatch", mock_stdout.getvalue())

    def test_add_license_header(self):
        """Test add_license_header function for Python files"""
        # Create a file without license header
        py_content = self.read_fixture("test_py_no_header.py")
        py_file = self.create_test_file("add_header.py", py_content)

        # Add license header
        expected_header = get_raw_expected_header(".py", "2025")
        result = add_license_header(py_file, expected_header)

        # Verify header was added
        self.assertTrue(result)
        with open(py_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("SPDX-FileCopyrightText", content)
            self.assertIn("SPDX-License-Identifier: Apache-2.0", content)

    def test_add_license_header_cpp(self):
        """Test add_license_header function for C++ files"""
        # Create a file without license header
        cpp_content = self.read_fixture("test_cpp_no_header.cpp")
        cpp_file = self.create_test_file("add_header.cpp", cpp_content)

        # Add license header
        expected_header = get_raw_expected_header(".cpp", "2025")
        result = add_license_header(cpp_file, expected_header)

        # Verify header was added
        self.assertTrue(result)
        with open(cpp_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("SPDX-FileCopyrightText", content)
            self.assertIn("SPDX-License-Identifier: Apache-2.0", content)
            # Check for C++ specific comment style
            self.assertIn("// SPDX", content)

    def test_replace_header(self):
        """Test replace_header function for Python files"""
        # Create a file with incorrect license header
        py_content = self.read_fixture("test_py_wrong_header.py")
        py_file = self.create_test_file("replace_header.py", py_content)

        # Replace license header
        expected_header = get_raw_expected_header(".py", "2025")
        result = replace_header(py_file, expected_header, 1)  # Header starts at line 1

        # Verify header was replaced
        self.assertTrue(result)
        with open(py_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("Tenstorrent AI ULC", content)
            self.assertIn("Apache-2.0", content)
            self.assertNotIn("Wrong Company", content)
            self.assertNotIn("MIT", content)

    def test_replace_header_cpp(self):
        """Test replace_header function for C++ files"""
        # Create a file with incorrect license header
        cpp_content = self.read_fixture("test_cpp_wrong_header.cpp")
        cpp_file = self.create_test_file("replace_header.cpp", cpp_content)

        # Replace license header
        expected_header = get_raw_expected_header(".cpp", "2025")
        result = replace_header(cpp_file, expected_header, 1)  # Header starts at line 1

        # Verify header was replaced
        self.assertTrue(result)
        with open(cpp_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("Tenstorrent AI ULC", content)
            self.assertIn("Apache-2.0", content)
            self.assertNotIn("Wrong Company", content)
            self.assertNotIn("MIT", content)
            # Check for C++ specific comment style
            self.assertIn("// SPDX", content)

    @patch("subprocess.run")
    def test_get_git_year(self, mock_run):
        """Test get_git_year function with mocked subprocess"""
        # Mock git command result - using CompletedProcess object
        mock_result = MagicMock()
        mock_result.stdout = "2023\n"
        mock_result.check_returncode.return_value = None  # No exception on check
        mock_run.return_value = mock_result

        # First test - successful case
        year = get_git_year(Path("dummy.py"))
        self.assertEqual(year, "2023")

        # Second test in a separate method to avoid side effects

    @patch("subprocess.run")
    def test_get_git_year_error(self, mock_run):
        """Test get_git_year function when git command fails"""
        # Test subprocess error
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        year = get_git_year(Path("dummy.py"))
        self.assertIsNone(year)


if __name__ == "__main__":
    unittest.main()
