#!/usr/bin/env python3
"""Upload a PNG into a Filester folder and verify its thumbnail_url.

Filester does not take a bare filename in JSON. Per https://filester.me/api-docs
you multipart-upload the image bytes with:

  POST /api/v1/upload
  Authorization: Bearer <FILESTER_API_KEY>
  X-Folder-ID: <folder_id>
  form field: file=@/path/to/image.png

Folder rows from GET /api/v1/folders then expose thumbnail_url (same field naming
as file list/detail responses).

Usage (reads .env on the VPS / locally):

  python scripts/test_filester_folder_thumbnail.py \\
    --folder 83ffc3af668a663b \\
    --image ./cover.png

If --image is omitted, the first *.png in the repo root is used.
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


def _default_png() -> str:
    matches = sorted(glob.glob(os.path.join(APP_DIR, "*.png")))
    return matches[0] if matches else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder",
        default="83ffc3af668a663b",
        help="Target Filester folder id (default: THUMBTEST folder)",
    )
    parser.add_argument(
        "--image",
        help="PNG/JPEG/WebP path to upload (default: first *.png in repo root)",
    )
    parser.add_argument(
        "--wait-sec",
        type=float,
        default=3.0,
        help="Seconds to wait before re-fetching folder thumbnail_url",
    )
    args = parser.parse_args()

    _load_dotenv()
    os.environ.setdefault("CACHE_DIR", os.path.join(APP_DIR, "cache"))

    import filester_upload

    folder_id = (args.folder or "").strip()
    image_path = (args.image or "").strip() or _default_png()
    if not image_path:
        print("error: no --image given and no *.png found in repo root", file=sys.stderr)
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
        print("Before: folder id not found in GET /api/v1/folders (will still try upload)")
    print()

    try:
        upload = filester_upload.upload_folder_thumbnail_image(folder_id, image_path)
    except Exception as exc:
        print(f"upload failed: {exc}", file=sys.stderr)
        return 1

    print("Upload response:")
    print(json.dumps(upload, indent=2))
    slug = filester_upload.file_identifier_from_response(upload)
    file_url = filester_upload.gallery_url_from_response(upload)
    if slug:
        print(f"Uploaded file id/slug: {slug}")
    if file_url:
        print(f"Uploaded file URL:     {file_url}")
    print()

    if args.wait_sec > 0:
        time.sleep(args.wait_sec)

    after = filester_upload.find_folder_row(folder_id)
    if not after:
        print("error: folder still not visible after upload", file=sys.stderr)
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
        "WARN — upload succeeded but folder thumbnail_url is still empty.\n"
        "       Filester may need more time to process the image, or the folder\n"
        "       cover may only update when the uploaded file is an image in an\n"
        "       otherwise-empty folder. Re-run with a longer --wait-sec or check\n"
        f"       {filester_upload.folder_url(folder_id)} in a browser."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
