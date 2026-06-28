"""Split oversized media into watchable parts via ffmpeg stream-copy.

No re-encoding ever happens: every part is produced with ``-c copy`` and keeps
the source container/codecs. Parts are named ``<name>.PART1.<ext>``,
``<name>.PART2.<ext>`` ... and each is independently playable
(``-reset_timestamps 1``). They concatenate back losslessly with:

    ffmpeg -f concat -safe 0 -i "concat:movie.PART1.mkv|movie.PART2.mkv" -c copy movie.mkv

Used in-process by apu and by the splitter-http sidecar.
"""
from __future__ import annotations

import math
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


def _copy_stream_maps() -> list[str]:
    """Maps for stream-copy splits: video + audio only.

    Sources often carry timecode/data/subtitle tracks that cannot be muxed into
    MP4 segment output (``codec none`` / ``Could not write header``). Browser
    parts only need A/V anyway.
    """
    return ["-map", "0:v", "-map", "0:a?"]


def _run_segment(path, output_dir, stem, ext, segment_time, timeout, on_log):
    pattern = os.path.join(output_dir, f"{stem}.PART%d{ext}")
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-y",
        "-i", path,
        *_copy_stream_maps(),
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


def iter_upload_parts(
    path: str,
    max_bytes: int,
    output_dir: str,
    *,
    on_log=None,
    should_cancel=None,
    delete_source: bool = True,
    ffmpeg_timeout: int = 7200,
):
    """Yield part dicts compatible with byte_splitter (ffmpeg PART naming).

    ffmpeg writes every part before upload begins, so peak disk is ~2× the file.
    Parts are independently playable in web players; rejoin with ffmpeg concat.
    """
    size = os.path.getsize(path)
    if size <= max_bytes:
        yield {
            "path": path,
            "filename": os.path.basename(path),
            "size_bytes": size,
            "part_index": 0,
            "part_count": 1,
            "is_source": True,
            "original_basename": os.path.basename(path),
            "split_mode": "ffmpeg",
        }
        return

    parts = split_file(
        path,
        max_bytes,
        output_dir,
        on_log=on_log,
        should_cancel=should_cancel,
        ffmpeg_timeout=ffmpeg_timeout,
    )
    original = os.path.basename(path)
    part_count = len(parts)
    for idx, part_path in enumerate(parts, start=1):
        yield {
            "path": part_path,
            "filename": os.path.basename(part_path),
            "size_bytes": os.path.getsize(part_path),
            "part_index": idx,
            "part_count": part_count,
            "is_source": False,
            "original_basename": original,
            "split_mode": "ffmpeg",
        }

    if delete_source:
        try:
            os.remove(path)
        except OSError:
            pass


def _extract_single_segment(
    path: str,
    output_path: str,
    start_sec: float,
    duration_sec: float,
    *,
    timeout: int,
    on_log=None,
) -> None:
    """Extract one stream-copy segment (playable output with normal extension)."""
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-y",
        "-ss", str(max(0.0, start_sec)),
        "-i", path,
        "-t", str(max(0.001, duration_sec)),
        *_copy_stream_maps(),
        "-c", "copy",
        "-reset_timestamps", "1",
        output_path,
    ]
    if on_log:
        on_log(
            f"ffmpeg slice {os.path.basename(output_path)} "
            f"@ {start_sec:.1f}s for {duration_sec:.1f}s"
        )
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-600:]
        raise SplitError(f"ffmpeg slice failed (exit {proc.returncode}): {tail}")


def iter_upload_parts_sliced(
    path: str,
    max_bytes: int,
    output_dir: str,
    *,
    on_log=None,
    should_cancel=None,
    delete_source: bool = True,
    ffmpeg_timeout: int = 7200,
):
    """Yield one ffmpeg stream-copy part at a time (~source + one part on disk).

    Parts use playable names (``movie.PART1.mp4``). Segment duration is tuned
    using the same byte budget as one-pass ffmpeg; the first part is probed before
    any uploads begin.
    """
    size = os.path.getsize(path)
    if size <= max_bytes:
        yield {
            "path": path,
            "filename": os.path.basename(path),
            "size_bytes": size,
            "part_index": 0,
            "part_count": 1,
            "is_source": True,
            "original_basename": os.path.basename(path),
            "split_mode": "ffmpeg_slice",
        }
        return

    os.makedirs(output_dir, exist_ok=True)
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    duration = probe_duration(path)
    bytes_per_sec = size / duration

    segment_time = None
    num_parts = None
    last_err = None
    for factor in _TARGET_FACTORS:
        if should_cancel and should_cancel():
            from downloader import TransferCancelled
            raise TransferCancelled("Upload cancelled")

        target_bytes = int(max_bytes * factor)
        trial_segment_time = max(1, int(target_bytes / bytes_per_sec))
        trial_num_parts = max(1, math.ceil(duration / trial_segment_time))
        probe_name = f"{stem}.PART1{ext}"
        probe_path = os.path.join(output_dir, probe_name)
        try:
            if os.path.isfile(probe_path):
                os.remove(probe_path)
        except OSError:
            pass

        _extract_single_segment(
            path,
            probe_path,
            0,
            trial_segment_time,
            timeout=ffmpeg_timeout,
            on_log=on_log,
        )
        probe_size = os.path.getsize(probe_path)
        try:
            os.remove(probe_path)
        except OSError:
            pass

        if probe_size > max_bytes:
            last_err = (
                f"first slice exceeded limit at factor {factor} "
                f"({probe_size:,} > {max_bytes:,} bytes)"
            )
            if on_log:
                on_log(last_err)
            continue

        segment_time = trial_segment_time
        num_parts = trial_num_parts
        if on_log:
            on_log(
                f"ffmpeg per-part slice: {num_parts} part(s), ~{segment_time}s each "
                f"(factor {factor})"
            )
        break

    if segment_time is None or num_parts is None:
        raise SplitError(
            f"Unable to slice {base} under {max_bytes:,} bytes. Last: {last_err}"
        )

    original = base
    for idx in range(num_parts):
        if should_cancel and should_cancel():
            from downloader import TransferCancelled
            raise TransferCancelled("Upload cancelled")

        start = idx * segment_time
        seg_dur = min(segment_time, duration - start)
        if seg_dur <= 0:
            break

        part_name = f"{stem}.PART{idx + 1}{ext}"
        part_path = os.path.join(output_dir, part_name)
        _extract_single_segment(
            path,
            part_path,
            start,
            seg_dur,
            timeout=ffmpeg_timeout,
            on_log=on_log,
        )
        part_size = os.path.getsize(part_path)
        if part_size > max_bytes:
            try:
                os.remove(part_path)
            except OSError:
                pass
            raise SplitError(
                f"Part {idx + 1} ({part_name}) is {part_size:,} bytes "
                f"(> {max_bytes:,}); try bytes mode or a smaller FILESTER_MAX_PART_BYTES"
            )

        yield {
            "path": part_path,
            "filename": part_name,
            "size_bytes": part_size,
            "part_index": idx + 1,
            "part_count": num_parts,
            "is_source": False,
            "original_basename": original,
            "split_mode": "ffmpeg_slice",
        }

    if delete_source:
        try:
            os.remove(path)
        except OSError:
            pass
