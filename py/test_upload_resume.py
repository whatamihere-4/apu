"""Tests for upload resume state persistence."""

from __future__ import annotations

import tempfile
import unittest

from upload_resume import (
    UploadedPart,
    UploadResumeState,
    delete_upload_resume_state,
    load_upload_resume_state,
    save_upload_resume_state,
)


class UploadResumeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job_dir = f"{self._tmp.name}/job-1"

    def test_round_trip_and_skip_indices(self) -> None:
        state = UploadResumeState(
            source_path="/downloads/movie.mp4",
            was_split=True,
            total_parts=3,
            parts=[
                UploadedPart(
                    part_index=1,
                    filename="movie.PART1.mp4",
                    size_bytes=1000,
                    slug="slug1",
                    upload_response={"slug": "slug1", "success": True},
                ),
            ],
        )
        save_upload_resume_state(self.job_dir, state)
        loaded = load_upload_resume_state(self.job_dir)
        assert loaded is not None
        self.assertEqual(loaded.total_parts, 3)
        self.assertEqual(loaded.skip_part_indices(), frozenset({1}))
        self.assertFalse(loaded.upload_complete())

    def test_append_part_if_new(self) -> None:
        state = UploadResumeState(total_parts=2)
        part = UploadedPart(1, "a.part001", 100, "slug-a")
        self.assertTrue(state.append_part_if_new(part))
        self.assertFalse(state.append_part_if_new(UploadedPart(1, "a.part001", 100, "slug-other")))
        self.assertEqual(len(state.parts), 1)

    def test_delete_state(self) -> None:
        save_upload_resume_state(self.job_dir, UploadResumeState())
        delete_upload_resume_state(self.job_dir)
        self.assertIsNone(load_upload_resume_state(self.job_dir))


if __name__ == "__main__":
    unittest.main()
