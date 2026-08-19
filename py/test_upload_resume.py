"""Tests for upload resume state persistence."""

from __future__ import annotations

import os
import tempfile
import unittest

from upload_resume import (
    UploadedPart,
    UploadResumeState,
    cleanup_split_artifacts,
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

    def test_cleanup_split_artifacts(self) -> None:
        source = f"{self._tmp.name}/movie.mp4"
        with open(source, "wb") as f:
            f.write(b"x")
        split_dir = f"{self._tmp.name}/.split_job-1"
        os.makedirs(split_dir)
        with open(f"{split_dir}/movie.PART1.mp4", "wb") as f:
            f.write(b"part")
        with open(f"{self._tmp.name}/movie.part001", "wb") as f:
            f.write(b"byte")
        save_upload_resume_state(self.job_dir, UploadResumeState())

        cleanup_split_artifacts(source, "job-1", resume_dir=self.job_dir)

        self.assertTrue(os.path.isfile(source))
        self.assertFalse(os.path.isdir(split_dir))
        self.assertFalse(os.path.isfile(f"{self._tmp.name}/movie.part001"))
        self.assertFalse(os.path.isdir(self.job_dir))


if __name__ == "__main__":
    unittest.main()
