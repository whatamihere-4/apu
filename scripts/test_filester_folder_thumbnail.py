#!/usr/bin/env python3
"""Set a Filester folder cover image and verify thumbnail_url.

Important: ``POST /api/v1/upload`` with ``X-Folder-ID`` adds a file *into* the folder.
That is not the same as setting the folder cover/thumbnail shown on /f/<id> pages.

Folder rows from ``GET /api/v1/folders`` expose ``thumbnail_url`` once a cover exists.
The public docs describe reading that field; the write endpoint may still be rolling
out — this script probes likely v1 paths and prints before/after folder metadata.

Usage:

  python scripts/test_filester_folder_thumbnail.py \\
    --folder 83ffc3af668a663b \\
    --image ./your.jpg

If --image is omitted, the first *.png/*.jpg in the repo root is used.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(APP_DIR, "py")
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

ENV_PATH = os.path.join(APP_DIR, ".env")


def _load_dotenv() -> None:
    if not os.path.isfile(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _default_image() -> str:
    for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        matches = sorted(glob.glob(os.path.join(APP_DIR, pattern)))
        if matches:
            return matches[0]
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder",
        default="83ffc3af668a663b",
        help="Target Filester folder id (default: THUMBTEST folder)",
    )
    parser.add_argument(
        "--image",
        help="Cover image path (default: first image in repo root)",
    )
    parser.add_argument(
        "--wait-sec",
        type=float,
        default=2.0,
        help="Seconds to wait before re-fetching folder thumbnail_url",
    )
    args = parser.parse_args()

    _load_dotenv()
    os.environ.setdefault("CACHE_DIR", os.path.join(APP_DIR, "cache"))

    import filester_upload

    folder_id = (args.folder or "").strip()
    image_path = (args.image or "").strip() or _default_image()
    if not image_path:
        print("error: no --image given and no image found in repo root", file=sys.stderr)
        return 1
    if not os.path.isfile(image_path):
        print(f"error: image not found: {image_path}", file=sys.stderr)
        return 1
    if not filester_upload.FILESTER_API_KEY:
        print("error: FILESTER_API_KEY is not set (.env or environment)", file=sys.stderr)
        return 1

    print(f"Filester base: {filester_upload.FILESTER_BASE_URL}")
    print(f"Folder:        {folder_id} ({filester_upload.folder_url(folder_id)})")
    print(f"Image:         {image_path} ({os.path.getsize(image_path):,} bytes)")
    print()

    before = filester_upload.find_folder_row(folder_id)
    if before:
        print(
            "Before:",
            json.dumps(
                {
                    "name": before.get("name"),
                    "file_count": before.get("file_count"),
                    "thumbnail_url": before.get("thumbnail_url"),
                },
                indent=2,
            ),
        )
    else:
        print("Before: folder id not found in GET /api/v1/folders")
    print()

    try:
        result = filester_upload.set_folder_thumbnail(folder_id, image_path)
    except Exception as exc:
        print(f"thumbnail upload failed: {exc}", file=sys.stderr)
        print(
            "\nIf this 404s, Filester may not have shipped the v1 thumbnail endpoint yet.\n"
            "Open folder Edit in the web manager, set a cover, and check DevTools → Network\n"
            "for the real path (likely a non-/api/v1/ route today).",
            file=sys.stderr,
        )
        return 1

    print("Thumbnail response:")
    print(json.dumps(result, indent=2))
    print()

    if args.wait_sec > 0:
        time.sleep(args.wait_sec)

    after = filester_upload.find_folder_row(folder_id)
    if not after:
        print("error: folder not visible after thumbnail upload", file=sys.stderr)
        return 1

    thumb = filester_upload.folder_thumbnail_url(after)
    print("After:")
    print(
        json.dumps(
            {
                "name": after.get("name"),
                "file_count": after.get("file_count"),
                "thumbnail_url": after.get("thumbnail_url"),
                "thumbnail_abs": thumb or None,
            },
            indent=2,
        )
    )
    print()
    if thumb:
        print(f"OK — folder thumbnail_url is set: {thumb}")
        return 0

    print(
        "WARN — thumbnail call succeeded but folder thumbnail_url is still empty.\n"
        f"Check {filester_upload.folder_url(folder_id)} in a browser."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
