"""Split large files into byte-range parts for Filester's ~10 GB upload limit.

Only one part exists on disk alongside the source at any moment (source + one part
peak). Parts are named ``<stem>.part001<ext>``, ``<stem>.part002<ext>``, … and
reassemble losslessly with ``cat`` (Linux) or ``copy /b`` (Windows).
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from pathlib import Path

_CHUNK_SIZE = 8 * 1024 * 1024
_SKIP_CHECK_EVERY_CHUNKS = 32


class SplitError(RuntimeError):
    pass


def required_disk_bytes(file_size: int, part_size_bytes: int) -> int:
    """Peak bytes on disk while processing one job (source + at most one part)."""
    if file_size <= 0:
        return 0
    if file_size <= part_size_bytes:
        return file_size
    return file_size + part_size_bytes


def _extract_part(
    source: Path,
    dest: Path,
    offset: int,
    size: int,
    skip_check: Callable[[], None] | None = None,
) -> None:
    with source.open("rb") as src, dest.open("wb") as dst:
        src.seek(offset)
        remaining = size
        chunks = 0
        while remaining > 0:
            if skip_check and chunks % _SKIP_CHECK_EVERY_CHUNKS == 0:
                skip_check()
            chunk = src.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                raise SplitError(
                    f"Short read extracting {dest.name} at offset {offset}"
                )
            dst.write(chunk)
            remaining -= len(chunk)
            chunks += 1


def iter_upload_parts(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    base_name: str | None = None,
    skip_check: Callable[[], None] | None = None,
    *,
    delete_source: bool = True,
) -> Iterator[dict]:
    """
    Yield upload parts one at a time.

    Only one part file exists on disk alongside the source at any moment.
    The consumer should upload each part and delete it before requesting the next.
    When ``delete_source`` is True (default), the source file is removed after all
    parts are yielded. Set False when another upload still needs the source file.
    """
    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(f"Source file not found: {source}")

    total_size = source.stat().st_size
    if total_size <= part_size_bytes:
        yield {
            "path": str(source),
            "filename": source.name,
            "size_bytes": total_size,
            "part_index": 0,
            "part_count": 1,
            "is_source": True,
            "original_basename": source.name,
        }
        return

    stem = base_name or source.stem
    suffix = source.suffix
    num_parts = math.ceil(total_size / part_size_bytes)

    for idx in range(num_parts):
        offset = idx * part_size_bytes
        part_size = min(part_size_bytes, total_size - offset)
        part_name = f"{stem}.part{idx + 1:03d}{suffix}"
        part_path = output_dir / part_name
        _extract_part(source, part_path, offset, part_size, skip_check=skip_check)
        yield {
            "path": str(part_path),
            "filename": part_name,
            "size_bytes": part_size,
            "part_index": idx + 1,
            "part_count": num_parts,
            "is_source": False,
            "original_basename": source.name,
        }

    if delete_source:
        source.unlink()
