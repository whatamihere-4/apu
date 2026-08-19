"""Tests for split-upload disk budget checks."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PY_DIR = Path(__file__).resolve().parent
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

import size_limits
from byte_splitter import additional_disk_bytes, required_disk_bytes

_GIB = 1024**3
_PART = 10_200_547_328  # default Filester part cap (~9.5 GiB)


class DiskByteModelTests(unittest.TestCase):
    def test_additional_ignores_source_for_byte_split(self) -> None:
        source = int(18 * _GIB)
        self.assertEqual(
            additional_disk_bytes(source, _PART, split_mode="bytes"),
            _PART,
        )
        self.assertEqual(
            required_disk_bytes(source, _PART, split_mode="bytes"),
            source + _PART,
        )

    def test_additional_for_ffmpeg_is_one_copy_of_parts(self) -> None:
        source = int(18 * _GIB)
        self.assertEqual(
            additional_disk_bytes(source, _PART, split_mode="ffmpeg"),
            source,
        )


class InsufficientDiskTests(unittest.TestCase):
    @patch.object(size_limits, "MIN_FREE_DISK_GB", 1.0)
    @patch.object(size_limits, "free_disk_gb", return_value=26.7)
    def test_existing_18gb_source_passes_with_27gb_free(self, _free) -> None:
        source = int(18 * _GIB)
        self.assertIsNone(
            size_limits.insufficient_disk_reason(
                source,
                _PART,
                download_dir="/downloads",
                split_mode="bytes",
            )
        )

    @patch.object(size_limits, "MIN_FREE_DISK_GB", 1.0)
    @patch.object(size_limits, "free_disk_gb", return_value=26.7)
    def test_old_formula_would_have_failed(self, _free) -> None:
        source = int(18 * _GIB)
        need_old = (
            required_disk_bytes(source, _PART, split_mode="bytes") / _GIB + 1.0
        )
        self.assertGreater(need_old, 26.7)

    @patch.object(size_limits, "MIN_FREE_DISK_GB", 1.0)
    @patch.object(size_limits, "free_disk_gb", return_value=10.0)
    def test_fails_when_scratch_part_does_not_fit(self, _free) -> None:
        source = int(18 * _GIB)
        reason = size_limits.insufficient_disk_reason(
            source,
            _PART,
            download_dir="/downloads",
            split_mode="bytes",
        )
        self.assertIsNotNone(reason)
        self.assertIn("Insufficient disk space", reason or "")

    @patch.object(size_limits, "AUTO_SKIP_OVERSIZED", True)
    @patch.object(size_limits, "MIN_FREE_DISK_GB", 1.0)
    @patch.object(size_limits, "free_disk_gb", return_value=26.7)
    @patch.object(size_limits, "DISK_BUDGET_GB", 45.0)
    def test_oversize_does_not_reject_downloaded_byte_split(self, _free) -> None:
        source = int(18 * _GIB)
        self.assertIsNone(
            size_limits.oversize_skip_reason(
                source,
                _PART,
                download_dir="/downloads",
                split_mode="bytes",
            )
        )


if __name__ == "__main__":
    unittest.main()
