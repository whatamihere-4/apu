#!/usr/bin/env python3
"""Sync cache/filester-folders.json from Filester GET /api/v1/folders.

Requires FILESTER_API_KEY in .env (or the environment).

Usage:
  python scripts/sync_filester_folders.py              # replace local map with API
  python scripts/sync_filester_folders.py --merge    # merge API into local map
"""

from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(SCRIPT_DIR)
PY_DIR = os.path.join(APP_DIR, "py")
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

ENV_PATH = os.path.join(APP_DIR, ".env")
OUT_PATH = os.path.join(APP_DIR, "cache", "filester-folders.json")


def _load_dotenv(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge API folders into the existing JSON (default: replace)",
    )
    parser.add_argument(
        "--output",
        default=OUT_PATH,
        help=f"Output JSON path (default: {OUT_PATH})",
    )
    args = parser.parse_args()

    _load_dotenv(ENV_PATH)
    os.environ.setdefault(
        "CACHE_DIR",
        os.path.join(APP_DIR, "cache"),
    )

    import json

    import filester_upload

    mode = "merge" if args.merge else "replace"
    try:
        remote = filester_upload.fetch_folder_map_from_api()
        remote = filester_upload.apply_folder_blacklist(remote)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if mode == "merge" and os.path.isfile(args.output):
        try:
            with open(args.output, encoding="utf-8") as f:
                local = json.load(f)
            if isinstance(local, dict):
                local.update(remote)
                remote = local
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: could not read {args.output} for merge: {exc}", file=sys.stderr)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(remote, f, indent=2, sort_keys=True)

    print(f"Wrote {len(remote)} folder(s) to {args.output} ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
