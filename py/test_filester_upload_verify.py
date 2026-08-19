"""Tests for Filester upload folder-listing recovery."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PY_DIR = Path(__file__).resolve().parent
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

import filester_upload as fs


class FileRowMatchTests(unittest.TestCase):
    def test_matches_name_and_size(self) -> None:
        row = {"name": "clip.PART1.mp4", "size": 1000}
        self.assertTrue(fs._file_row_matches_upload(row, "clip.PART1.mp4", 1000))

    def test_rejects_wrong_size(self) -> None:
        row = {"name": "clip.PART1.mp4", "size": 999}
        self.assertFalse(fs._file_row_matches_upload(row, "clip.PART1.mp4", 1000))

    def test_rejects_wrong_name(self) -> None:
        row = {"name": "other.mp4", "size": 1000}
        self.assertFalse(fs._file_row_matches_upload(row, "clip.PART1.mp4", 1000))


class UploadResponseFromListingTests(unittest.TestCase):
    def test_builds_slug_and_url(self) -> None:
        out = fs.upload_response_from_folder_file(
            {
                "name": "clip.PART1.mp4",
                "size": 1000,
                "url": "https://filester.me/d/abc123",
                "id": 42,
            }
        )
        self.assertTrue(out["success"])
        self.assertEqual(out["slug"], "abc123")
        self.assertEqual(out["file_id"], 42)
        self.assertTrue(out["verified_via_folder_listing"])


class FindUploadedFileTests(unittest.TestCase):
    @patch.object(fs, "list_folder_files")
    def test_find_returns_matching_row(self, list_mock) -> None:
        list_mock.return_value = [
            {"name": "a.mp4", "size": 1},
            {"name": "b.PART1.mp4", "size": 500},
        ]
        row = fs.find_uploaded_file_in_folder("folder-1", "b.PART1.mp4", 500)
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "b.PART1.mp4")


class UploadVerifyRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_after = fs.FILESTER_UPLOAD_VERIFY_AFTER_SEC
        self._orig_poll = fs.FILESTER_UPLOAD_VERIFY_POLL_SEC
        self._orig_enabled = fs.FILESTER_UPLOAD_VERIFY_ENABLED
        fs.FILESTER_UPLOAD_VERIFY_AFTER_SEC = 0
        fs.FILESTER_UPLOAD_VERIFY_POLL_SEC = 1
        fs.FILESTER_UPLOAD_VERIFY_ENABLED = True

    def tearDown(self) -> None:
        fs.FILESTER_UPLOAD_VERIFY_AFTER_SEC = self._orig_after
        fs.FILESTER_UPLOAD_VERIFY_POLL_SEC = self._orig_poll
        fs.FILESTER_UPLOAD_VERIFY_ENABLED = self._orig_enabled

    @patch.object(fs, "find_uploaded_file_in_folder")
    @patch.object(fs, "_upload_post_once")
    def test_recovers_when_ack_hangs(self, post_mock, find_mock) -> None:
        started = threading.Event()

        def slow_post(*_a, **kwargs):
            started.set()
            bytes_sent_at = kwargs.get("bytes_sent_at")
            if bytes_sent_at is not None:
                bytes_sent_at[0] = time.time()
            time.sleep(30)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"success": True, "slug": "late"}
            return resp

        post_mock.side_effect = slow_post
        find_mock.return_value = {
            "name": "clip.PART1.mp4",
            "size": 123,
            "url": "https://filester.me/d/recovered",
            "id": 9,
        }

        out = fs._wait_for_upload_response(
            "/tmp/clip.PART1.mp4",
            filename="clip.PART1.mp4",
            filesize=123,
            folder_id="studio-folder",
            on_progress=None,
            should_cancel=None,
        )
        self.assertTrue(started.wait(2))
        self.assertIsInstance(out, dict)
        self.assertEqual(out["slug"], "recovered")
        self.assertTrue(out["verified_via_folder_listing"])

    @patch.object(fs, "find_uploaded_file_in_folder")
    @patch.object(fs, "_upload_post_once")
    def test_recovers_when_post_errors_but_file_landed(self, post_mock, find_mock) -> None:
        def fail_post(*_a, **kwargs):
            bytes_sent_at = kwargs.get("bytes_sent_at")
            if bytes_sent_at is not None:
                bytes_sent_at[0] = time.time()
            raise RuntimeError("connection reset")

        post_mock.side_effect = fail_post
        find_mock.return_value = {
            "name": "clip.PART1.mp4",
            "size": 123,
            "url": "https://filester.me/d/recovered",
            "id": 9,
        }

        out = fs._wait_for_upload_response(
            "/tmp/clip.PART1.mp4",
            filename="clip.PART1.mp4",
            filesize=123,
            folder_id="studio-folder",
            on_progress=None,
            should_cancel=None,
        )
        self.assertIsInstance(out, dict)
        self.assertEqual(out["slug"], "recovered")
        find_mock.assert_called()


if __name__ == "__main__":
    unittest.main()
