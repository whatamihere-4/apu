"""Tests for iter_upload_parts resume/skip handling."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PY_DIR = Path(__file__).resolve().parent
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from file_splitter import iter_upload_parts


class IterUploadPartsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source = os.path.join(self._tmp.name, "movie.mp4")
        with open(self.source, "wb") as f:
            f.write(b"0" * 1024)
        self.out_dir = os.path.join(self._tmp.name, ".split_test")

    def test_delete_source_with_empty_skip_indices(self) -> None:
        parts = [
            os.path.join(self.out_dir, "movie.PART1.mp4"),
            os.path.join(self.out_dir, "movie.PART2.mp4"),
        ]
        os.makedirs(self.out_dir, exist_ok=True)
        for part in parts:
            with open(part, "wb") as f:
                f.write(b"x")

        with patch("file_splitter.split_file", return_value=parts):
            yielded = list(
                iter_upload_parts(
                    self.source,
                    512,
                    self.out_dir,
                    delete_source=True,
                    skip_part_indices=frozenset(),
                )
            )

        self.assertEqual(len(yielded), 2)
        self.assertFalse(os.path.isfile(self.source))

    def test_skips_already_uploaded_parts(self) -> None:
        parts = [
            os.path.join(self.out_dir, "movie.PART1.mp4"),
            os.path.join(self.out_dir, "movie.PART2.mp4"),
        ]
        os.makedirs(self.out_dir, exist_ok=True)
        for part in parts:
            with open(part, "wb") as f:
                f.write(b"x")

        with patch("file_splitter.split_file", return_value=parts) as split_mock:
            yielded = list(
                iter_upload_parts(
                    self.source,
                    512,
                    self.out_dir,
                    delete_source=True,
                    skip_part_indices=frozenset({1}),
                )
            )

        split_mock.assert_called_once()
        self.assertEqual([p["part_index"] for p in yielded], [2])
        self.assertTrue(os.path.isfile(self.source))


if __name__ == "__main__":
    unittest.main()
