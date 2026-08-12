"""Unit tests for Filester studio folder sync filtering."""

from __future__ import annotations

import unittest

import filester_upload as fs


class FilterSyncFolderRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_root_id = fs.FILESTER_ROOT_FOLDER_ID
        self._orig_root_name = fs.FILESTER_ROOT_FOLDER_NAME

    def tearDown(self) -> None:
        fs.FILESTER_ROOT_FOLDER_ID = self._orig_root_id
        fs.FILESTER_ROOT_FOLDER_NAME = self._orig_root_name

    def test_studio_folders_only_under_vr_root(self) -> None:
        fs.FILESTER_ROOT_FOLDER_ID = "vr-root"
        rows = [
            {"id": "vr-root", "name": "VR", "parent": None},
            {"id": "studio-a", "name": "CzechVR", "parent": "vr-root"},
            {"id": "studio-b", "name": "VirtualPapi", "parent": "vr-root"},
            {
                "id": "split-sub",
                "name": "My Scene.mp4",
                "parent": "studio-a",
            },
        ]
        picked = fs._filter_sync_folder_rows(rows, include_children=True)
        self.assertEqual(
            {fs._folder_row_id(r) for r in picked},
            {"studio-a", "studio-b"},
        )

    def test_account_root_when_include_children_off(self) -> None:
        rows = [
            {"id": "vr-root", "name": "VR", "parent": None},
            {"id": "studio-a", "name": "CzechVR", "parent": "vr-root"},
        ]
        picked = fs._filter_sync_folder_rows(rows, include_children=False)
        self.assertEqual([fs._folder_row_id(r) for r in picked], ["vr-root"])

    def test_auto_detect_vr_root_by_name(self) -> None:
        fs.FILESTER_ROOT_FOLDER_ID = ""
        fs.FILESTER_ROOT_FOLDER_NAME = "VR"
        rows = [
            {"id": "vr-root", "name": "VR", "parent": None},
            {"id": "studio-a", "name": "Testing", "parent": "vr-root"},
        ]
        picked = fs._filter_sync_folder_rows(rows, include_children=True)
        self.assertEqual([fs._folder_row_id(r) for r in picked], ["studio-a"])


if __name__ == "__main__":
    unittest.main()
