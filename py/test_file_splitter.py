"""Unit tests for keyframe-aligned ffmpeg_slice planning."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PY_DIR = Path(__file__).resolve().parent
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from file_splitter import (
    _estimate_segment_bytes,
    _format_mkvmerge_time,
    _format_read_interval,
    _mkvmerge_split_spec,
    _scaled_segment_timeout,
    _select_keyframe_at_or_after,
    _validate_planned_parts,
    format_mkvmerge_rejoin_command,
    plan_keyframe_part_starts,
    plan_sparse_keyframe_part_starts,
)


class KeyframeSplitPlanTests(unittest.TestCase):
    def test_single_part_when_target_exceeds_duration(self) -> None:
        kf = [0.0, 10.0, 20.0, 30.0]
        self.assertEqual(
            plan_keyframe_part_starts(kf, duration=25.0, target_segment_time=60),
            [0.0],
        )

    def test_aligns_to_next_keyframe_after_target(self) -> None:
        kf = [0.0, 10.0, 20.0, 30.0, 60.0]
        self.assertEqual(
            plan_keyframe_part_starts(kf, duration=55.0, target_segment_time=15),
            [0.0, 20.0],
        )

    def test_skips_duplicate_keyframe_times(self) -> None:
        kf = [0.0, 10.0, 10.0, 25.0, 40.0]
        self.assertEqual(
            plan_keyframe_part_starts(kf, duration=50.0, target_segment_time=12),
            [0.0, 25.0, 40.0],
        )

    def test_inserts_zero_when_first_keyframe_is_late(self) -> None:
        kf = [2.0, 12.0, 24.0]
        self.assertEqual(
            plan_keyframe_part_starts(kf, duration=30.0, target_segment_time=10),
            [0.0, 12.0, 24.0],
        )


class MkvmergeHelperTests(unittest.TestCase):
    def test_format_mkvmerge_time(self) -> None:
        self.assertEqual(_format_mkvmerge_time(0.0), "00:00:00.000")
        self.assertEqual(_format_mkvmerge_time(3661.5), "01:01:01.500")

    def test_split_spec_open_end(self) -> None:
        self.assertEqual(
            _mkvmerge_split_spec(10.0, 100.0, duration=100.0),
            "parts:00:00:10.000-",
        )

    def test_split_spec_closed_end(self) -> None:
        self.assertEqual(
            _mkvmerge_split_spec(10.0, 50.0, duration=100.0),
            "parts:00:00:10.000-00:00:50.000",
        )

    def test_rejoin_command(self) -> None:
        self.assertEqual(
            format_mkvmerge_rejoin_command("movie", ".mp4", 3),
            "mkvmerge -o movie.mp4 movie.PART1.mp4 +movie.PART2.mp4 +movie.PART3.mp4",
        )


class ValidatePlannedPartsTests(unittest.TestCase):
    def test_rejects_oversized_estimate_for_any_part(self) -> None:
        # Two equal halves of a 22 GiB file at average bitrate: each ~11 GiB estimate.
        duration = 7200.0
        size = 22 * 1024**3
        bytes_per_sec = size / duration
        starts = [0.0, 3600.0]
        ok, err = _validate_planned_parts(
            "/nonexistent",
            starts,
            duration,
            10 * 1024**3,
            bytes_per_sec=bytes_per_sec,
            probe_timeout=60,
        )
        self.assertFalse(ok)
        self.assertIn("estimate", err or "")

    def test_accepts_small_parts_without_probe(self) -> None:
        duration = 7200.0
        size = 5 * 1024**3
        bytes_per_sec = size / duration
        starts = [0.0, 3600.0]
        ok, err = _validate_planned_parts(
            "/nonexistent",
            starts,
            duration,
            10 * 1024**3,
            bytes_per_sec=bytes_per_sec,
            probe_timeout=60,
        )
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_estimate_segment_bytes_applies_margin(self) -> None:
        raw = 1000.0 * 1_000_000
        est = _estimate_segment_bytes(0.0, 1000.0, 1_000_000.0)
        self.assertEqual(est, int(raw * 1.10))


class SparsePlanTests(unittest.TestCase):
    def test_sparse_plan_delegates_to_keyframe_plan_when_mocked(self) -> None:
        self.assertEqual(
            plan_sparse_keyframe_part_starts("/nonexistent", duration=0.0, target_segment_time=60),
            [0.0],
        )

    def test_scaled_segment_timeout_caps_at_max(self) -> None:
        timeout = _scaled_segment_timeout(
            segment_sec=1200.0,
            file_size=30 * 1024**3,
            duration=3600.0,
            max_timeout=1800,
        )
        self.assertLessEqual(timeout, 1800)
        self.assertGreaterEqual(timeout, 300)

    def test_format_read_interval_window(self) -> None:
        self.assertEqual(_format_read_interval(100.0, 200.0, 1000.0), "100.000%200.000")

    def test_select_keyframe_at_or_after(self) -> None:
        kf = [0.0, 10.0, 20.0, 30.0]
        self.assertEqual(_select_keyframe_at_or_after(kf, 15.0), 20.0)
        self.assertEqual(_select_keyframe_at_or_after(kf, 30.0), 30.0)
        self.assertIsNone(_select_keyframe_at_or_after(kf, 31.0))


if __name__ == "__main__":
    unittest.main()
