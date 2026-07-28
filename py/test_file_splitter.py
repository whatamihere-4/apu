"""Unit tests for keyframe-aligned ffmpeg_slice planning."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PY_DIR = Path(__file__).resolve().parent
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from file_splitter import plan_keyframe_part_starts


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


if __name__ == "__main__":
    unittest.main()
