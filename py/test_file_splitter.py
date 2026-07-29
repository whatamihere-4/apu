"""Unit tests for keyframe-aligned ffmpeg_slice planning."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PY_DIR = Path(__file__).resolve().parent
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from file_splitter import (
    _format_mkvmerge_time,
    _mkvmerge_split_spec,
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


class MkvmergeHelperTests(unittest.TestCase):
    def test_format_mkvmerge_time(self) -> None:
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


class SparsePlanTests(unittest.TestCase):
    def test_sparse_plan_delegates_to_keyframe_plan_when_mocked(self) -> None:
        # plan_sparse_keyframe_part_starts needs ffprobe; verify it returns [0.0]
        # for zero-duration edge case without touching the filesystem.
        self.assertEqual(
            plan_sparse_keyframe_part_starts("/nonexistent", duration=0.0, target_segment_time=60),
            [0.0],
        )


if __name__ == "__main__":
    unittest.main()
