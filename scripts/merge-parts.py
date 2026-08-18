#!/usr/bin/env python3
"""Merge ``name.PART1.ext``, ``name.PART2.ext``, … with ffmpeg concat (stream copy).

Usage (run in the folder that contains the PART files):

    python3 merge-parts.py
    python3 merge-parts.py /path/to/parts/folder
    python3 merge-parts.py /path/to/parts/folder -o merged.mp4

Requires: ffmpeg on PATH.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
_PART_RE = re.compile(r"^(?P<stem>.+)\.PART(?P<num>\d+)(?P<ext>\..+)$", re.IGNORECASE)


class MergeError(RuntimeError):
    pass


def discover_part_groups(directory: Path) -> list[tuple[str, list[Path]]]:
    groups: dict[str, list[tuple[int, Path]]] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        match = _PART_RE.match(path.name)
        if not match:
            continue
        key = match.group("stem") + match.group("ext")
        groups.setdefault(key, []).append((int(match.group("num")), path))

    found: list[tuple[str, list[Path]]] = []
    for key, items in sorted(groups.items()):
        items.sort(key=lambda t: t[0])
        nums = [n for n, _ in items]
        if not nums or nums[0] != 1 or nums != list(range(1, len(nums) + 1)):
            continue
        found.append((key, [p for _, p in items]))
    return found


def merge_parts(parts: list[Path], output: Path, *, verbose: bool = True) -> Path:
    if len(parts) == 1:
        raise MergeError("Only one part file; nothing to merge")
    if output.exists():
        raise MergeError(f"Refusing to overwrite existing file: {output}")

    list_path = output.parent / ".parts.txt"
    try:
        lines = [f"file '{part.name}'" for part in parts]
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
        if verbose:
            print("running:", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise MergeError(f"ffmpeg failed (exit {proc.returncode}): {tail}")
    finally:
        list_path.unlink(missing_ok=True)

    if verbose:
        print(f"merged -> {output}")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge .PART1/.PART2… files with ffmpeg concat.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Folder containing PART files (default: current directory)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output file path (default: <stem><ext> from PART1)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print errors",
    )
    args = parser.parse_args(argv)

    directory = args.directory.resolve()
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 1

    groups = discover_part_groups(directory)
    if not groups:
        print(f"error: no .PART1.* files found in {directory}", file=sys.stderr)
        return 1
    if len(groups) > 1:
        print(
            "error: multiple PART sets found; merge one folder at a time:",
            file=sys.stderr,
        )
        for key, parts in groups:
            print(f"  {key}: {len(parts)} part(s)", file=sys.stderr)
        return 1

    output_name, parts = groups[0]
    output = args.output.resolve() if args.output else directory / output_name
    verbose = not args.quiet

    try:
        merge_parts(parts, output, verbose=verbose)
    except MergeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
