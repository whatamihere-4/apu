"""Tests for upload speed watchdog phase gating."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from upload_watchdog import run_watchdog_once, watchdog_settings


class UploadWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = self._tmp.name

    @patch.dict("os.environ", {"UPLOAD_WATCHDOG_ENABLED": "true", "UPLOAD_WATCHDOG_COOLDOWN_SEC": "0"}, clear=False)
    def test_splitting_phase_is_ignored(self) -> None:
        jobs = {
            "abc": {
                "status": "uploading",
                "progress": {"phase": "splitting", "speed": 1024},
            }
        }
        result = run_watchdog_once(jobs, self.cache_dir)
        self.assertEqual(result["action"], "idle")
        self.assertFalse(result["restart_required"])

    @patch.dict("os.environ", {"UPLOAD_WATCHDOG_ENABLED": "true", "UPLOAD_WATCHDOG_COOLDOWN_SEC": "0"}, clear=False)
    def test_slow_upload_can_restart(self) -> None:
        jobs = {
            "abc": {
                "status": "uploading",
                "progress": {"phase": "uploading", "speed": 100 * 1024},
                "source_path": "/downloads/big.mp4",
                "job_kind": "path",
            }
        }
        state = {"low_since": 1, "last_job_id": "abc"}
        with patch("upload_watchdog._load_state", return_value=state):
            with patch("upload_watchdog._save_state"):
                result = run_watchdog_once(jobs, self.cache_dir)
        self.assertEqual(result["action"], "restart")
        self.assertTrue(result["restart_required"])


if __name__ == "__main__":
    unittest.main()
