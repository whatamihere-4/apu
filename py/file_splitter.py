"""Split oversized media into watchable parts via ffmpeg stream-copy.

No re-encoding ever happens: every part is produced with ``-c copy`` and keeps
the source container/codecs. Parts are named ``<name>.PART1.<ext>``,
``<name>.PART2.<ext>`` ... and each is independently playable
(``-reset_timestamps 1``). They concatenate back losslessly with:

    ffmpeg -f concat -safe 0 -i "concat:movie.PART1.mkv|movie.PART2.mkv" -c copy movie.mkv

Used in-process by gofup and by the splitter-http sidecar.
"""
from __future__ import annotations

import os
import subprocess

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
# Target a fraction of the limit so keyframe-boundary overshoot stays under it.
_TARGET_FACTORS = (0.90, 0.75, 0.60)


class SplitError(RuntimeError):
    pass


def probe_duration(path: str) -> float:
    proc = subprocess.run(
        [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    raw = (proc.stdout or "").strip()
    try:
        dur = float(raw)
    except ValueError:
        dur = 0.0
    if dur <= 0:
        raise SplitError(
            f"Could not determine media duration via ffprobe (got {raw!r}); cannot split {os.path.basename(path)}"
        )
    return dur


def _part_paths(output_dir: str, stem: str, ext: str) -> list[str]:
    """Return produced parts sorted by their numeric PART index."""
    parts = []
    for name in os.listdir(output_dir):
        if not name.startswith(f"{stem}.PART") or not name.endswith(ext):
            continue
        mid = name[len(f"{stem}.PART"):-len(ext)] if ext else name[len(f"{stem}.PART"):]
        if mid.isdigit():
            parts.append((int(mid), os.path.join(output_dir, name)))
    parts.sort(key=lambda t: t[0])
    return [p for _, p in parts]


def _run_segment(path, output_dir, stem, ext, segment_time, timeout, on_log):
    pattern = os.path.join(output_dir, f"{stem}.PART%d{ext}")
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-y",
        "-i", path,
        "-map", "0",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-reset_timestamps", "1",
        "-segment_start_number", "1",
        pattern,
    ]
    if on_log:
        on_log(f"ffmpeg segment (stream copy), ~{segment_time}s per part")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-600:]
        raise SplitError(f"ffmpeg split failed (exit {proc.returncode}): {tail}")


def split_file(
    path: str,
    max_bytes: int,
    output_dir: str,
    *,
    on_log=None,
    should_cancel=None,
    ffmpeg_timeout: int = 7200,
) -> list[str]:
    """Split ``path`` so every part is <= ``max_bytes``. Returns ordered part paths.

    If the file is already within the limit, returns ``[path]`` unchanged.
    """
    size = os.path.getsize(path)
    if size <= max_bytes:
        return [path]

    os.makedirs(output_dir, exist_ok=True)
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    duration = probe_duration(path)
    bytes_per_sec = size / duration

    last_err = None
    for factor in _TARGET_FACTORS:
        if should_cancel and should_cancel():
            from downloader import TransferCancelled
            raise TransferCancelled("Upload cancelled")
        # Clear any parts from a previous (overshooting) attempt.
        for stale in _part_paths(output_dir, stem, ext):
            try:
                os.remove(stale)
            except OSError:
                pass

        target_bytes = int(max_bytes * factor)
        segment_time = max(1, int(target_bytes / bytes_per_sec))
        _run_segment(path, output_dir, stem, ext, segment_time, ffmpeg_timeout, on_log)

        parts = _part_paths(output_dir, stem, ext)
        if not parts:
            raise SplitError("ffmpeg produced no output parts")
        oversized = [p for p in parts if os.path.getsize(p) > max_bytes]
        if not oversized:
            if on_log:
                for p in parts:
                    on_log(f"part {os.path.basename(p)} = {os.path.getsize(p):,} bytes")
            return parts
        last_err = (
            f"{len(oversized)} part(s) exceeded the limit at factor {factor}; retrying with smaller segments"
        )
        if on_log:
            on_log(last_err)

    raise SplitError(
        f"Unable to split {base} under {max_bytes:,} bytes after retries. Last: {last_err}"
    )
