"""Disk and source-file size limits for Filester split uploads."""
from __future__ import annotations

import os
import shutil

from byte_splitter import required_disk_bytes

_GIB = 1024**3


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


MIN_FREE_DISK_GB = _env_float("MIN_FREE_DISK_GB", 5.0)
DISK_BUDGET_GB = _env_float("DISK_BUDGET_GB", 0.0)
MAX_SOURCE_FILE_BYTES = _env_int("MAX_SOURCE_FILE_BYTES", 0)
MAX_SOURCE_FILE_GB = _env_float("MAX_SOURCE_FILE_GB", 0.0)
AUTO_SKIP_OVERSIZED = _env_bool("AUTO_SKIP_OVERSIZED", True)


def free_disk_gb(path: str) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / _GIB


def max_processable_source_bytes(
    part_size_bytes: int,
    *,
    download_dir: str,
    free_gb: float | None = None,
    split_mode: str = "bytes",
) -> int:
    """Largest single source file we can process on available disk."""
    if MAX_SOURCE_FILE_BYTES > 0:
        return MAX_SOURCE_FILE_BYTES
    if MAX_SOURCE_FILE_GB > 0:
        return int(MAX_SOURCE_FILE_GB * _GIB)

    if DISK_BUDGET_GB > 0:
        budget_gb = float(DISK_BUDGET_GB)
    else:
        budget_gb = free_gb if free_gb is not None else free_disk_gb(download_dir)

    usable_gb = budget_gb - MIN_FREE_DISK_GB
    if split_mode == "ffmpeg":
        return int(max(0.0, usable_gb / 2.0) * _GIB)
    part_gb = part_size_bytes / _GIB
    return int(max(0.0, usable_gb - part_gb) * _GIB)


def required_disk_gb(
    file_size: int,
    part_size_bytes: int,
    *,
    split_mode: str = "bytes",
) -> float:
    if file_size <= 0:
        return float(MIN_FREE_DISK_GB)
    return (
        required_disk_bytes(file_size, part_size_bytes, split_mode=split_mode) / _GIB
        + MIN_FREE_DISK_GB
    )


def oversize_skip_reason(
    file_size: int,
    part_size_bytes: int,
    *,
    download_dir: str,
    split_mode: str = "bytes",
) -> str | None:
    if not AUTO_SKIP_OVERSIZED or file_size <= 0:
        return None
    limit = max_processable_source_bytes(
        part_size_bytes, download_dir=download_dir, split_mode=split_mode
    )
    if limit <= 0:
        return "File size unknown or disk budget too small"
    if file_size > limit:
        return (
            f"File too large for disk budget "
            f"({file_size / _GIB:.1f} GiB > {limit / _GIB:.1f} GiB max)"
        )
    return None


def insufficient_disk_reason(
    file_size: int,
    part_size_bytes: int,
    *,
    download_dir: str,
    split_mode: str = "bytes",
) -> str | None:
    """Return an error message when free space is below what this file needs."""
    need_gb = required_disk_gb(file_size, part_size_bytes, split_mode=split_mode)
    have_gb = free_disk_gb(download_dir)
    if have_gb < need_gb:
        if split_mode == "ffmpeg":
            detail = f"source + all parts (~2× file) + {MIN_FREE_DISK_GB:.0f} GiB headroom"
        else:
            detail = f"source + one part + {MIN_FREE_DISK_GB:.0f} GiB headroom"
        return (
            f"Insufficient disk space: need {need_gb:.1f} GiB free ({detail}), "
            f"have {have_gb:.1f} GiB"
        )
    return None
