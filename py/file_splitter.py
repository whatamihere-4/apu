"""Split oversized media into watchable parts via ffmpeg stream-copy.

No re-encoding ever happens: every part is produced with ``-c copy`` and keeps
the source container/codecs. Parts are named ``<name>.PART1.<ext>``,
``<name>.PART2.<ext>`` ... and each is independently playable. Splits are
aligned to video keyframes so parts rejoin cleanly (no re-encode). The
``ffmpeg_slice`` path extracts one part at a time (~source + one part on disk);
default backend is ffmpeg input seek, with legacy mkvmerge fifo available via
``SPLITTER_EXTRACT_BACKEND=mkvmerge``. Rejoin for phash-identical output::

    mkvmerge -o movie.mp4 movie.PART1.mp4 +movie.PART2.mp4 +movie.PART3.mp4

Or with ffmpeg concat demuxer (stream copy, may differ from source phash)::

    ffmpeg -f concat -safe 0 -i parts.txt -c copy movie.mp4

Used in-process by apu and by the splitter-http sidecar.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
MKVMERGE_BIN = os.environ.get("MKVMERGE_BIN", "mkvmerge")
# Target a fraction of the limit so keyframe-boundary overshoot stays under it.
_TARGET_FACTORS = (0.90, 0.75, 0.60)
_KEYFRAME_EPS = 0.001
_SPARSE_INITIAL_WINDOW_SEC = 180.0
_SPARSE_MAX_WINDOW_SEC = 300.0
_SPARSE_LOOKBACK_SEC = 30.0
_SPARSE_FORWARD_STEP_SEC = 150.0
_FULL_SCAN_MAX_BYTES = 15 * 1024**3
_PROBE_SIZE_MARGIN = 1.10
_PROBE_SKIP_ESTIMATE_RATIO = 0.85
_MIN_SEGMENT_TIMEOUT_SEC = 300
_EXTRACT_FLOOR_BPS = 2 * 1024 * 1024  # pessimistic VPS read+write for stream copy


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


FFPROBE_KEYFRAME_TIMEOUT = max(60, _env_int("SPLITTER_FFPROBE_TIMEOUT_SEC", 300))
SPLITTER_EXTRACT_BACKEND = (
    (os.environ.get("SPLITTER_EXTRACT_BACKEND") or "ffmpeg").strip().lower() or "ffmpeg"
)


def _scaled_segment_timeout(
    segment_sec: float,
    file_size: int,
    duration: float,
    max_timeout: int,
) -> int:
    """Cap per-segment extract time from segment bytes, not the global default."""
    if segment_sec <= 0 or duration <= 0 or file_size <= 0:
        return min(max_timeout, _MIN_SEGMENT_TIMEOUT_SEC)
    segment_bytes = int(file_size * (segment_sec / duration))
    io_budget = segment_bytes / _EXTRACT_FLOOR_BPS
    scaled = int(max(_MIN_SEGMENT_TIMEOUT_SEC, io_budget * 2.0))
    return min(max_timeout, scaled)


def _probe_timeout_for_file(file_size: int, configured: int) -> int:
    """Scale ffprobe keyframe probes down on huge files so planning fails fast."""
    size_gb = file_size / (1024**3)
    per_gb = max(60, configured // 4)
    return min(configured, max(60, int(size_gb * per_gb)))


def format_mkvmerge_rejoin_command(
    stem: str,
    ext: str,
    part_count: int,
    *,
    output_name: str | None = None,
) -> str:
    """Single mkvmerge command to rejoin playable PART files (phash-identical)."""
    out = output_name or f"{stem}{ext}"
    parts = [f"{stem}.PART{i}{ext}" for i in range(1, part_count + 1)]
    if part_count <= 1:
        return parts[0]
    return f"{MKVMERGE_BIN} -o {out} {parts[0]} " + " ".join(f"+{p}" for p in parts[1:])


class SplitError(RuntimeError):
    pass


class _KeyframeCache:
    """Accumulate keyframe PTS values across sparse probes for one file."""

    def __init__(self) -> None:
        self._times: set[float] = {0.0}

    def add(self, times: list[float]) -> None:
        self._times.update(times)

    def at_or_after(self, target_sec: float) -> float | None:
        return _select_keyframe_at_or_after(sorted(self._times), target_sec)


def _check_cancel(should_cancel) -> None:
    if should_cancel and should_cancel():
        from downloader import TransferCancelled
        raise TransferCancelled("Upload cancelled")


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


def _parse_keyframe_times(stdout: str) -> list[float]:
    times: list[float] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = float(line)
        except ValueError:
            continue
        if t >= -_KEYFRAME_EPS:
            times.append(t)
    return times


def _keyframes_from_packets(path: str, *, probe_timeout: int = 3600) -> list[float]:
    proc = subprocess.run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=probe_timeout,
    )
    if proc.returncode != 0:
        return []

    times: list[float] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",", 1)
        if len(parts) != 2 or "K" not in parts[1]:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            continue
        if t >= -_KEYFRAME_EPS:
            times.append(t)
    return times


def probe_keyframe_times(path: str, *, probe_timeout: int = 3600) -> list[float]:
    """Return sorted presentation timestamps of video keyframes."""
    proc = subprocess.run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-skip_frame",
            "nokey",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=probe_timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:]
        raise SplitError(f"ffprobe keyframe scan failed for {os.path.basename(path)}: {tail}")

    times = _parse_keyframe_times(proc.stdout or "")
    if not times:
        times = _keyframes_from_packets(path, probe_timeout=probe_timeout)

    if not times:
        raise SplitError(f"No video keyframes found in {os.path.basename(path)}")

    times = sorted(set(times))
    if times[0] > _KEYFRAME_EPS:
        times = [0.0, *times]
    return times


def _format_read_interval(start_sec: float, end_sec: float, duration: float) -> str:
    if duration <= 0:
        raise SplitError("Cannot build read interval for zero-duration media")
    start_sec = max(0.0, min(start_sec, duration))
    end_sec = max(start_sec, min(end_sec, duration))
    if end_sec <= start_sec + _KEYFRAME_EPS:
        end_sec = min(duration, start_sec + 1.0)
    return f"{start_sec:.3f}%{end_sec:.3f}"


def _keyframes_from_packets_in_interval(
    path: str,
    *,
    start_sec: float,
    end_sec: float,
    probe_timeout: int = 600,
) -> list[float]:
    proc = subprocess.run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-read_intervals",
            f"{start_sec:.6f}%{end_sec:.6f}",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=probe_timeout,
    )
    if proc.returncode != 0:
        return []

    times: list[float] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",", 1)
        if len(parts) != 2 or "K" not in parts[1]:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            continue
        if start_sec - _KEYFRAME_EPS <= t <= end_sec + _KEYFRAME_EPS:
            times.append(t)
    return times


def _probe_keyframes_in_interval(
    path: str,
    *,
    start_sec: float,
    end_sec: float,
    duration: float,
    probe_timeout: int = 300,
) -> list[float]:
    if duration <= 0:
        return []
    start_sec = max(0.0, start_sec)
    end_sec = min(duration, end_sec)
    if end_sec <= start_sec + _KEYFRAME_EPS:
        return []

    interval = _format_read_interval(start_sec, end_sec, duration)
    proc = subprocess.run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-read_intervals",
            interval,
            "-skip_frame",
            "nokey",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=probe_timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:]
        raise SplitError(
            f"ffprobe keyframe window failed for {os.path.basename(path)} ({interval}): {tail}"
        )

    times = _parse_keyframe_times(proc.stdout or "")
    if not times:
        times = _keyframes_from_packets_in_interval(
            path, start_sec=start_sec, end_sec=end_sec, probe_timeout=probe_timeout
        )
    return sorted(set(times))


def _select_keyframe_at_or_after(times: list[float], target_sec: float) -> float | None:
    candidates = [t for t in sorted(times) if t >= target_sec - _KEYFRAME_EPS]
    return candidates[0] if candidates else None


def find_keyframe_at_or_after(
    path: str,
    target_sec: float,
    duration: float,
    target_segment_time: float,
    *,
    cache: _KeyframeCache | None = None,
    probe_timeout: int = 300,
    file_size: int = 0,
) -> float | None:
    if duration <= 0:
        return None

    target_sec = max(0.0, min(target_sec, duration))
    if cache is not None:
        cached = cache.at_or_after(target_sec)
        if cached is not None:
            return cached

    window = min(
        _SPARSE_MAX_WINDOW_SEC,
        max(_SPARSE_INITIAL_WINDOW_SEC, min(target_segment_time * 0.15, 240.0)),
    )
    cursor = max(0.0, target_sec - _SPARSE_LOOKBACK_SEC)

    while cursor < duration - _KEYFRAME_EPS:
        end = min(duration, cursor + window)
        times = _probe_keyframes_in_interval(
            path, start_sec=cursor, end_sec=end, duration=duration,
            probe_timeout=probe_timeout,
        )
        if cache is not None:
            cache.add(times)

        picked = _select_keyframe_at_or_after(times, target_sec)
        if picked is not None:
            return picked

        if end >= duration - _KEYFRAME_EPS:
            break
        cursor = max(cursor + _SPARSE_FORWARD_STEP_SEC, end - _SPARSE_LOOKBACK_SEC)

    if file_size > _FULL_SCAN_MAX_BYTES:
        raise SplitError(
            f"Sparse keyframe lookup missed target {target_sec:.3f}s in "
            f"{os.path.basename(path)}; refusing full-file ffprobe on "
            f"{file_size / (1024**3):.1f} GiB source"
        )

    full = probe_keyframe_times(path, probe_timeout=probe_timeout * 3)
    if cache is not None:
        cache.add(full)
    return _select_keyframe_at_or_after(full, target_sec)


def plan_keyframe_part_starts(
    keyframes: list[float],
    duration: float,
    target_segment_time: float,
) -> list[float]:
    """Return part start times ``[0, kf1, kf2, ...]`` on keyframe boundaries."""
    if duration <= 0:
        return [0.0]

    kf = sorted(set(keyframes))
    if kf[0] > _KEYFRAME_EPS:
        kf = [0.0, *kf]

    starts = [0.0]
    while starts[-1] < duration - _KEYFRAME_EPS:
        target = starts[-1] + max(1.0, target_segment_time)
        if target >= duration - _KEYFRAME_EPS:
            break

        candidates = [t for t in kf if t >= target - _KEYFRAME_EPS]
        if not candidates:
            break

        next_start = candidates[0]
        if next_start <= starts[-1] + _KEYFRAME_EPS:
            later = [t for t in kf if t > starts[-1] + _KEYFRAME_EPS]
            if not later:
                break
            next_start = later[0]

        if next_start >= duration - _KEYFRAME_EPS:
            break

        starts.append(next_start)

    return starts


def plan_sparse_keyframe_part_starts(
    path: str,
    duration: float,
    target_segment_time: float,
    *,
    probe_timeout: int = 300,
    file_size: int = 0,
) -> list[float]:
    """Plan part starts by probing only near each split boundary."""
    if duration <= 0:
        return [0.0]

    cache = _KeyframeCache()
    starts = [0.0]
    while starts[-1] < duration - _KEYFRAME_EPS:
        target = starts[-1] + max(1.0, target_segment_time)
        if target >= duration - _KEYFRAME_EPS:
            break

        next_start = find_keyframe_at_or_after(
            path, target, duration, target_segment_time, cache=cache,
            probe_timeout=probe_timeout, file_size=file_size,
        )
        if next_start is None:
            break
        if next_start <= starts[-1] + _KEYFRAME_EPS:
            next_start = find_keyframe_at_or_after(
                path,
                starts[-1] + _KEYFRAME_EPS,
                duration,
                target_segment_time,
                cache=cache,
                probe_timeout=probe_timeout,
                file_size=file_size,
            )
            if next_start is None or next_start <= starts[-1] + _KEYFRAME_EPS:
                break

        if next_start >= duration - _KEYFRAME_EPS:
            break

        starts.append(next_start)

    return starts


def _format_mkvmerge_time(sec: float) -> str:
    sec = max(0.0, sec)
    hours = int(sec // 3600)
    minutes = int((sec % 3600) // 60)
    seconds = sec % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def _mkvmerge_split_spec(start_sec: float, end_sec: float, duration: float) -> str:
    if end_sec >= duration - _KEYFRAME_EPS:
        return f"parts:{_format_mkvmerge_time(start_sec)}-"
    return f"parts:{_format_mkvmerge_time(start_sec)}-{_format_mkvmerge_time(end_sec)}"


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


def _ffmpeg_line_for_log(line: str) -> str | None:
    """Pick stderr lines worth surfacing in the job log (drop libav banner noise)."""
    s = line.strip()
    if not s:
        return None
    lower = s.lower()
    if lower.startswith("ffmpeg version") or lower.startswith("configuration:"):
        return None
    if "libav" in lower and ("copyright" in lower or "built with" in lower):
        return None
    if "input #" in lower and "from '" in lower:
        return None
    if "output #" in lower and "to '" in lower:
        return s
    if "opening '" in lower or "stream mapping" in lower:
        return s
    if "error" in lower or "failed" in lower or "warning" in lower:
        return s
    if "time=" in s and ("frame=" in s or "size=" in s or "bitrate=" in s):
        return s
    if s.startswith("frame="):
        return s
    return None


def _run_ffmpeg_logged(
    cmd: list[str],
    *,
    timeout: int,
    on_log=None,
    should_cancel=None,
) -> None:
    """Run ffmpeg, streaming filtered stderr lines to ``on_log``."""
    if "-stats_period" not in cmd:
        # Periodic progress on stderr when not attached to a TTY.
        insert_at = 1 if len(cmd) > 1 and cmd[1] == "-hide_banner" else 1
        cmd = cmd[:insert_at] + ["-stats_period", "1"] + cmd[insert_at:]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_buf: list[str] = []
    last_progress = [0.0]

    def _reader() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_buf.append(line)
            if not on_log:
                continue
            picked = _ffmpeg_line_for_log(line)
            if not picked:
                continue
            now = time.time()
            if "time=" in picked and (now - last_progress[0]) < 1.0:
                continue
            if "time=" in picked:
                last_progress[0] = now
            on_log(f"[ffmpeg] {picked}")

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    deadline = time.time() + timeout
    rc = None
    try:
        while True:
            if should_cancel and should_cancel():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                from downloader import TransferCancelled
                raise TransferCancelled("Upload cancelled")
            rc = proc.poll()
            if rc is not None:
                break
            if time.time() > deadline:
                proc.kill()
                proc.wait()
                raise SplitError(f"ffmpeg timed out after {timeout}s")
            time.sleep(0.25)
    finally:
        reader.join(timeout=3)

    if rc != 0:
        tail = "".join(stderr_buf)[-600:]
        raise SplitError(f"ffmpeg failed (exit {rc}): {tail}")


def _run_segment(path, output_dir, stem, ext, segment_time, timeout, on_log, should_cancel=None):
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
    _run_ffmpeg_logged(
        cmd, timeout=timeout, on_log=on_log, should_cancel=should_cancel,
    )


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
        _run_segment(
            path, output_dir, stem, ext, segment_time, ffmpeg_timeout, on_log,
            should_cancel=should_cancel,
        )

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


def _extract_single_segment_mkvmerge(
    path: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    *,
    duration: float,
    timeout: int,
    on_log=None,
    should_cancel=None,
) -> None:
    """Extract ``[start_sec, end_sec)`` via mkvmerge → fifo → ffmpeg (legacy, slow on MP4)."""
    if end_sec - start_sec <= _KEYFRAME_EPS:
        raise SplitError(
            f"Refusing zero-length segment for {os.path.basename(output_path)} "
            f"({start_sec:.3f}s–{end_sec:.3f}s)"
        )

    spec = _mkvmerge_split_spec(start_sec, end_sec, duration)
    fifo = output_path + ".fifo"
    try:
        if os.path.exists(fifo):
            os.unlink(fifo)
    except OSError:
        pass
    os.mkfifo(fifo)

    ffmpeg_cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-y",
        "-i",
        fifo,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]
    if on_log:
        on_log(f"mkvmerge→fifo→ffmpeg {os.path.basename(output_path)} {spec}")

    ff_proc: subprocess.Popen[str] | None = None
    try:
        ff_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _check_cancel(should_cancel)

        mkv_cmd = [
            MKVMERGE_BIN,
            "-q",
            "-o",
            fifo,
            "--split",
            spec,
            path,
        ]
        deadline = time.time() + timeout
        try:
            mkv_proc = subprocess.run(
                mkv_cmd,
                capture_output=True,
                text=True,
                timeout=max(1, int(deadline - time.time())),
            )
        except subprocess.TimeoutExpired as exc:
            raise SplitError(f"mkvmerge timed out after {timeout}s") from exc

        assert ff_proc is not None
        try:
            _, ff_stderr = ff_proc.communicate(timeout=max(1, int(deadline - time.time())))
        except subprocess.TimeoutExpired as exc:
            ff_proc.kill()
            ff_proc.communicate()
            raise SplitError(f"ffmpeg timed out after {timeout}s") from exc

        if ff_proc.returncode != 0:
            tail = (ff_stderr or "")[-600:]
            mkv_tail = (mkv_proc.stderr or mkv_proc.stdout or "")[-300:]
            raise SplitError(
                f"ffmpeg remux failed (exit {ff_proc.returncode}) for "
                f"{os.path.basename(output_path)}: {tail}; "
                f"mkvmerge exit {mkv_proc.returncode}: {mkv_tail}"
            )

        if mkv_proc.returncode >= 2:
            pass  # mkvmerge fifo exit 2 is expected after mux finishes
        elif mkv_proc.returncode == 1 and on_log:
            tail = (mkv_proc.stderr or mkv_proc.stdout or "").strip()
            if tail:
                on_log(f"mkvmerge warnings for {os.path.basename(output_path)}: {tail[-400:]}")

        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise SplitError(f"ffmpeg produced no output for {os.path.basename(output_path)}")
    finally:
        if ff_proc is not None and ff_proc.poll() is None:
            ff_proc.kill()
            ff_proc.communicate()
        try:
            if os.path.exists(fifo):
                os.unlink(fifo)
        except OSError:
            pass


def _extract_single_segment_ffmpeg(
    path: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    *,
    duration: float,
    timeout: int,
    on_log=None,
    should_cancel=None,
) -> None:
    """Extract ``[start_sec, end_sec)`` via ffmpeg input seek + stream copy (fast on MP4)."""
    if end_sec - start_sec <= _KEYFRAME_EPS:
        raise SplitError(
            f"Refusing zero-length segment for {os.path.basename(output_path)} "
            f"({start_sec:.3f}s–{end_sec:.3f}s)"
        )

    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-y",
        "-ss",
        f"{start_sec:.6f}",
    ]
    if end_sec < duration - _KEYFRAME_EPS:
        cmd.extend(["-to", f"{end_sec:.6f}"])
    cmd.extend(
        [
            "-i",
            path,
            *_copy_stream_maps(),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output_path,
        ]
    )
    if on_log:
        on_log(
            f"ffmpeg stream-copy {os.path.basename(output_path)} "
            f"{start_sec:.3f}s–{end_sec:.3f}s (timeout {timeout}s)"
        )
    _run_ffmpeg_logged(cmd, timeout=timeout, on_log=on_log, should_cancel=should_cancel)


def _extract_single_segment(
    path: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    *,
    duration: float,
    file_size: int,
    max_timeout: int,
    extract_backend: str | None = None,
    on_log=None,
    should_cancel=None,
) -> None:
    segment_sec = end_sec - start_sec
    seg_timeout = _scaled_segment_timeout(segment_sec, file_size, duration, max_timeout)
    backend = (extract_backend or SPLITTER_EXTRACT_BACKEND or "ffmpeg").strip().lower()
    if backend == "mkvmerge":
        _extract_single_segment_mkvmerge(
            path,
            output_path,
            start_sec,
            end_sec,
            duration=duration,
            timeout=seg_timeout,
            on_log=on_log,
            should_cancel=should_cancel,
        )
        return
    _extract_single_segment_ffmpeg(
        path,
        output_path,
        start_sec,
        end_sec,
        duration=duration,
        timeout=seg_timeout,
        on_log=on_log,
        should_cancel=should_cancel,
    )


def iter_upload_parts_sliced(
    path: str,
    max_bytes: int,
    output_dir: str,
    *,
    on_log=None,
    should_cancel=None,
    delete_source: bool = True,
    ffmpeg_timeout: int = 1800,
    ffprobe_keyframe_timeout: int | None = None,
    extract_backend: str | None = None,
):
    """Yield one ffmpeg-sliced part at a time (~source + one part on disk).

    Parts use playable names (``movie.PART1.mp4``). Segment duration is tuned
    using sparse keyframe probes; the first part is extracted before uploads begin.
    Rejoin with :func:`format_mkvmerge_rejoin_command` for phash-identical output.
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
    probe_timeout = _probe_timeout_for_file(
        size, ffprobe_keyframe_timeout or FFPROBE_KEYFRAME_TIMEOUT
    )
    backend = extract_backend or SPLITTER_EXTRACT_BACKEND

    segment_time = None
    part_starts = None
    last_err = None
    for factor in _TARGET_FACTORS:
        _check_cancel(should_cancel)

        target_bytes = int(max_bytes * factor)
        trial_segment_time = max(1, int(target_bytes / bytes_per_sec))
        if on_log:
            on_log(
                f"Planning sparse keyframe split for {base} "
                f"(~{trial_segment_time}s target, factor {factor})"
            )
        trial_starts = plan_sparse_keyframe_part_starts(
            path, duration, trial_segment_time,
            probe_timeout=probe_timeout, file_size=size,
        )
        probe_name = f"{stem}.PART1{ext}"
        probe_path = os.path.join(output_dir, probe_name)
        try:
            if os.path.isfile(probe_path):
                os.remove(probe_path)
        except OSError:
            pass

        first_end = trial_starts[1] if len(trial_starts) > 1 else duration
        est_probe_size = int((first_end - trial_starts[0]) * bytes_per_sec * _PROBE_SIZE_MARGIN)
        probe_skip_threshold = int(max_bytes * _PROBE_SKIP_ESTIMATE_RATIO)
        if est_probe_size > probe_skip_threshold:
            _extract_single_segment(
                path,
                probe_path,
                0,
                first_end,
                duration=duration,
                file_size=size,
                max_timeout=ffmpeg_timeout,
                extract_backend=backend,
                on_log=on_log,
                should_cancel=should_cancel,
            )
            probe_size = os.path.getsize(probe_path)
            try:
                os.remove(probe_path)
            except OSError:
                pass
        else:
            if on_log:
                on_log(
                    f"Skipping probe slice (est {est_probe_size:,} bytes "
                    f"≤ {probe_skip_threshold:,} threshold)"
                )
            probe_size = est_probe_size

        if probe_size > max_bytes:
            last_err = (
                f"first slice exceeded limit at factor {factor} "
                f"({probe_size:,} > {max_bytes:,} bytes)"
            )
            if on_log:
                on_log(last_err)
            continue

        segment_time = trial_segment_time
        part_starts = trial_starts
        if on_log:
            on_log(
                f"ffmpeg keyframe-aligned slice: {len(trial_starts)} part(s), "
                f"~{segment_time}s target (factor {factor})"
            )
        break

    if segment_time is None or part_starts is None:
        raise SplitError(
            f"Unable to slice {base} under {max_bytes:,} bytes. Last: {last_err}"
        )

    original = base
    num_parts = len(part_starts)
    for idx, start in enumerate(part_starts):
        _check_cancel(should_cancel)

        end = part_starts[idx + 1] if idx + 1 < num_parts else duration
        if end - start <= _KEYFRAME_EPS:
            continue

        part_name = f"{stem}.PART{idx + 1}{ext}"
        part_path = os.path.join(output_dir, part_name)
        if on_log:
            on_log(f"Splitting part {idx + 1}/{num_parts}: {part_name}")
        _extract_single_segment(
            path,
            part_path,
            start,
            end,
            duration=duration,
            file_size=size,
            max_timeout=ffmpeg_timeout,
            extract_backend=backend,
            on_log=on_log,
            should_cancel=should_cancel,
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
