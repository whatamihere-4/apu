#!/usr/bin/env python3
"""Generate thumbnail contact sheets for one or more videos.

This replaces the old thumbs.sh workflow with a Python wrapper around ffmpeg/ffprobe.
It keeps the same calling style:
  python3 thumber_worker.py [-o OUT.png] <video> [video ...]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

COLS = 4
ROWS = 5
FRAMES = COLS * ROWS
THUMB_W = 320
THUMB_H = 180
PADDING = 6
MARGIN = 16

FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
TIMEOUT_SEC = int(os.environ.get("THUMBER_FFMPEG_TIMEOUT_SLOW_SEC", "600"))


def _log(msg: str) -> None:
    print(f"[thumber] {msg}", flush=True)


def _resolve_input(path: str) -> str:
    if os.path.isabs(path) or "/" in path or "\\" in path:
        return path
    in_dir = (os.environ.get("THUMBER_IN_DIR") or "").rstrip("/")
    if in_dir:
        candidate = f"{in_dir}/{path}"
        if os.path.isfile(candidate):
            return candidate
    return path


def _resolve_output(path: str) -> str:
    if os.path.isabs(path) or "/" in path or "\\" in path:
        return path
    out_dir = (os.environ.get("THUMBER_OUT_DIR") or "").rstrip("/")
    if out_dir:
        return f"{out_dir}/{path}"
    return path


def _default_output_for_video(video_path: str) -> str:
    base = os.path.basename(video_path)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return _resolve_output(f"{stem}_thumbs.png")


def _ffprobe_duration(video_path: str) -> float:
    cmd = [
        FFPROBE_BIN,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        return 0.0
    raw = (cp.stdout or "").strip().splitlines()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw[0]))
    except ValueError:
        return 0.0


def _build_filter(duration: float) -> str:
    # For very short clips, keep fps sane while still filling the 4x5 tile.
    effective_duration = max(duration, 1.0)
    fps = FRAMES / effective_duration
    return (
        f"fps={fps:.6f},"
        f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease,"
        f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"tile={COLS}x{ROWS}:padding={PADDING}:margin={MARGIN}:color=#c4c4c4"
    )


def _generate_sheet(video_path: str, out_path: str) -> None:
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"input file not found: {video_path}")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    duration = _ffprobe_duration(video_path)
    vf = _build_filter(duration)
    cmd = [
        FFMPEG_BIN,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-vf",
        vf,
        "-frames:v",
        "1",
        out_path,
    ]
    started = time.monotonic()
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SEC)
    elapsed = time.monotonic() - started
    if cp.returncode != 0:
        stderr = (cp.stderr or "").strip()
        raise RuntimeError(f"ffmpeg failed ({cp.returncode}): {stderr[:500]}")
    _log(f"Done: {out_path} ({elapsed:.1f}s)")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="thumber_worker",
        description="Generate <stem>_thumbs.png sheets for videos",
    )
    parser.add_argument("-o", "--output", help="Output file path (single input only)")
    parser.add_argument("videos", nargs="+", help="Video files or basenames")
    args = parser.parse_args()

    if args.output and len(args.videos) != 1:
        print("error: -o/--output requires exactly one input video", file=sys.stderr)
        return 2

    try:
        if args.output:
            video = _resolve_input(args.videos[0])
            out = _resolve_output(args.output)
            _log(f"Processing: {os.path.basename(video)}")
            _generate_sheet(video, out)
            return 0

        for raw in args.videos:
            video = _resolve_input(raw)
            out = _default_output_for_video(video)
            _log(f"Processing: {os.path.basename(video)}")
            _generate_sheet(video, out)
        return 0
    except subprocess.TimeoutExpired:
        print(f"[thumber] TIMEOUT after {TIMEOUT_SEC}s", file=sys.stderr, flush=True)
        return 124
    except Exception as e:  # noqa: BLE001
        print(f"[thumber] ERROR: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
