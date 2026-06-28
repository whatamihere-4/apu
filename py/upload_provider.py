"""Upload orchestrator: one or both of GoFile and Filester.

Enable providers independently via GOFILE_ENABLED / FILESTER_ENABLED, or the
legacy UPLOAD_PROVIDER shorthand (gofile, filester, dual). Filester oversized
uploads split via FILESTER_SPLIT_MODE: bytes, ffmpeg (one-pass), ffmpeg_slice
(per-part), or optimal (one-pass when disk allows, else FILESTER_SPLIT_FALLBACK).
"""
from __future__ import annotations

import os
import shutil
import uuid

import byte_splitter
import file_splitter
import filester_upload
import gofile_upload
import size_limits
from upload_common import UploadResult, format_size  # noqa: F401 (re-exported)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


DOWNLOADS_DIR = os.path.realpath(
    (os.environ.get("MEDIA_DOWNLOADS_DIR") or "./downloads").rstrip("/") or "./downloads"
)
FILESTER_MAX_PART_BYTES = _env_int("FILESTER_MAX_PART_BYTES", 10_200_547_328)
FILESTER_FFMPEG_TIMEOUT = _env_int("SPLITTER_FFMPEG_TIMEOUT_SEC", 7200)

_SPLIT_MODE_ALIASES = {
    "splice": "bytes",
    "byte": "bytes",
    "bytes": "bytes",
    "cat": "bytes",
    "ffmpeg": "ffmpeg",
    "ffmpeg_onepass": "ffmpeg",
    "onepass": "ffmpeg",
    "one_pass": "ffmpeg",
    "ffmpeg_slice": "ffmpeg_slice",
    "ffmpeg-slice": "ffmpeg_slice",
    "slice": "ffmpeg_slice",
    "optimal": "optimal",
    "auto": "optimal",
}


def _parse_split_mode(raw: str, *, default: str = "bytes") -> str:
    mode = _SPLIT_MODE_ALIASES.get(raw.strip().lower())
    if mode:
        return mode
    print(
        f"[UPLOAD] Unknown split mode {raw!r}; using {default}",
        flush=True,
    )
    return default


_raw_split_mode = (os.environ.get("FILESTER_SPLIT_MODE") or "bytes").strip().lower()
FILESTER_SPLIT_MODE = _parse_split_mode(_raw_split_mode)

_raw_fallback = (os.environ.get("FILESTER_SPLIT_FALLBACK") or "bytes").strip().lower()
FILESTER_SPLIT_FALLBACK = _parse_split_mode(_raw_fallback, default="bytes")
if FILESTER_SPLIT_FALLBACK not in ("bytes", "ffmpeg_slice"):
    print(
        f"[UPLOAD] FILESTER_SPLIT_FALLBACK must be bytes or ffmpeg_slice; "
        f"got {_raw_fallback!r}, using bytes",
        flush=True,
    )
    FILESTER_SPLIT_FALLBACK = "bytes"


def resolve_split_mode(file_size: int) -> str:
    """Pick the effective split mode for a file (resolves optimal)."""
    if FILESTER_SPLIT_MODE != "optimal":
        return FILESTER_SPLIT_MODE
    if file_size <= FILESTER_MAX_PART_BYTES:
        return "bytes"
    if size_limits.insufficient_disk_reason(
        file_size,
        FILESTER_MAX_PART_BYTES,
        download_dir=DOWNLOADS_DIR,
        split_mode="ffmpeg",
    ) is None:
        return "ffmpeg"
    return FILESTER_SPLIT_FALLBACK


def _split_mode_label(mode: str) -> str:
    return {
        "bytes": "byte-range splice (one part on disk at a time)",
        "ffmpeg": "ffmpeg one-pass stream-copy (playable parts, ~2× disk during split)",
        "ffmpeg_slice": "ffmpeg per-part stream-copy (playable parts, one part on disk at a time)",
    }.get(mode, mode)

_legacy = (os.environ.get("UPLOAD_PROVIDER") or "").strip().lower()
if _legacy in ("dual", "both"):
    _default_gofile, _default_filester = True, True
elif _legacy == "filester":
    _default_gofile, _default_filester = False, True
else:
    _default_gofile, _default_filester = True, False

GOFILE_ENABLED = _env_bool("GOFILE_ENABLED", _default_gofile)
FILESTER_ENABLED = _env_bool("FILESTER_ENABLED", _default_filester)

if not GOFILE_ENABLED and not FILESTER_ENABLED:
    raise RuntimeError("At least one upload provider must be enabled (GOFILE_ENABLED / FILESTER_ENABLED)")

ACTIVE_PROVIDERS: list[str] = []
if GOFILE_ENABLED:
    ACTIVE_PROVIDERS.append("gofile")
if FILESTER_ENABLED:
    ACTIVE_PROVIDERS.append("filester")

_labels = []
if GOFILE_ENABLED:
    _labels.append("GoFile")
if FILESTER_ENABLED:
    _labels.append("Filester")
PROVIDER_LABEL = " + ".join(_labels)

UPLOAD_PROVIDER = "dual" if len(ACTIVE_PROVIDERS) > 1 else ACTIVE_PROVIDERS[0]

if GOFILE_ENABLED and not (os.environ.get("GOFILE_API_KEY") or "").strip():
    print("[UPLOAD] Warning: GOFILE_ENABLED but GOFILE_API_KEY is empty", flush=True)
if FILESTER_ENABLED and not (os.environ.get("FILESTER_API_KEY") or "").strip():
    print("[UPLOAD] Warning: FILESTER_ENABLED but FILESTER_API_KEY is empty", flush=True)


def _normalize_gofile(raw: dict) -> UploadResult:
    ok = raw.get("status") == "ok"
    url = (raw.get("data") or {}).get("downloadPage", "") if ok else ""
    return UploadResult(ok=ok, provider="gofile", gallery_url=url, raw=raw)


def _normalize_filester(raw: dict, *, part: dict | None = None) -> UploadResult:
    gallery_url = filester_upload.gallery_url_from_response(raw)
    ok = bool(raw.get("success")) and bool(gallery_url)
    part_count = 1
    part_index = 0
    original_basename = ""
    was_split = False
    split_mode = str(part.get("split_mode") or "") if part else ""
    if part:
        part_count = int(part.get("part_count") or 1)
        part_index = int(part.get("part_index") or 0)
        original_basename = str(part.get("original_basename") or "")
        was_split = part_count > 1
    return UploadResult(
        ok=ok,
        provider="filester",
        gallery_url=gallery_url,
        raw=raw,
        part_index=part_index,
        part_count=part_count,
        original_basename=original_basename,
        was_split=was_split,
        split_mode=split_mode,
    )


def get_root_folder_id():
    return gofile_upload.get_root_folder_id()


def create_folder(parent_id, name):
    return gofile_upload.create_folder(parent_id, name)


def folder_url(folder_id, *, provider: str = "gofile"):
    if provider == "filester":
        return filester_upload.folder_url(folder_id)
    return gofile_upload.folder_url(folder_id)


def plan_upload_destinations(file_size: int) -> tuple[list[str], str | None]:
    """Return (providers_to_use, filester_skip_reason).

    GoFile is included when enabled. Filester is skipped (not an error) when
    disk budget cannot accommodate splitting.
    """
    destinations: list[str] = []
    filester_skip: str | None = None

    if GOFILE_ENABLED:
        destinations.append("gofile")

    if FILESTER_ENABLED:
        mode = resolve_split_mode(file_size)
        skip = size_limits.oversize_skip_reason(
            file_size,
            FILESTER_MAX_PART_BYTES,
            download_dir=DOWNLOADS_DIR,
            split_mode=mode,
        )
        if skip:
            filester_skip = skip
        else:
            low = size_limits.insufficient_disk_reason(
                file_size,
                FILESTER_MAX_PART_BYTES,
                download_dir=DOWNLOADS_DIR,
                split_mode=mode,
            )
            if low:
                filester_skip = low
            else:
                destinations.append("filester")

    return destinations, filester_skip


def _expand_sources(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        out = []
        for root, _dirs, files in os.walk(path):
            for fname in sorted(files):
                out.append(os.path.join(root, fname))
        return out
    raise FileNotFoundError(f"Path not found: {path}")


def _upload_gofile(
    src: str,
    *,
    folder_id,
    on_progress,
    should_cancel,
) -> UploadResult:
    raw = gofile_upload.upload_file(
        src,
        folder_id=folder_id,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )
    return _normalize_gofile(raw)


def _upload_filester_parts(
    src: str,
    *,
    folder_id,
    on_progress,
    should_cancel,
    on_log,
    job_id,
    delete_source: bool,
) -> tuple[list[UploadResult], str | None]:
    from downloader import TransferCancelled

    size = os.path.getsize(src)
    results: list[UploadResult] = []
    needs_split = size > FILESTER_MAX_PART_BYTES
    split_mode = resolve_split_mode(size)
    upload_folder_id = (folder_id or "").strip() or None

    if not needs_split:
        raw = filester_upload.upload_file(
            src,
            folder_id=upload_folder_id,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
        results.append(_normalize_filester(raw))
        return results, upload_folder_id

    token = job_id or uuid.uuid4().hex[:8]
    out_dir = os.path.join(os.path.dirname(src) or ".", f".split_{token}")
    os.makedirs(out_dir, exist_ok=True)
    original_basename = os.path.basename(src)
    stem, _ext = os.path.splitext(original_basename)

    if upload_folder_id:
        try:
            subfolder_id = filester_upload.create_folder(upload_folder_id, stem or original_basename)
            upload_folder_id = subfolder_id
            if on_log:
                on_log(
                    f'[Filester] Created subfolder "{filester_upload.sanitize_folder_name(stem or original_basename)}" '
                    f"for split parts (parent studio folder)"
                )
        except Exception as e:
            if on_log:
                on_log(f"[Filester] Subfolder create failed ({e}); uploading parts to studio folder")
    if on_log:
        if FILESTER_SPLIT_MODE == "optimal" and needs_split:
            on_log(
                f"[Filester] optimal → {split_mode} for {os.path.basename(src)} "
                f"({format_size(size)})"
            )
        mode_label = _split_mode_label(split_mode)
        on_log(
            f"[Filester] {os.path.basename(src)} is {format_size(size)} "
            f"(> {format_size(FILESTER_MAX_PART_BYTES)}); splitting via {mode_label}"
        )
        need_gb = size_limits.required_disk_gb(
            size, FILESTER_MAX_PART_BYTES, split_mode=split_mode
        )
        on_log(f"[Filester] Split upload needs ~{need_gb:.1f} GiB peak disk")

    def _skip_check():
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")

    if split_mode == "ffmpeg":
        part_source = file_splitter.iter_upload_parts(
            src,
            FILESTER_MAX_PART_BYTES,
            out_dir,
            on_log=on_log,
            should_cancel=should_cancel,
            delete_source=delete_source,
            ffmpeg_timeout=FILESTER_FFMPEG_TIMEOUT,
        )
    elif split_mode == "ffmpeg_slice":
        part_source = file_splitter.iter_upload_parts_sliced(
            src,
            FILESTER_MAX_PART_BYTES,
            out_dir,
            on_log=on_log,
            should_cancel=should_cancel,
            delete_source=delete_source,
            ffmpeg_timeout=FILESTER_FFMPEG_TIMEOUT,
        )
    else:
        part_source = byte_splitter.iter_upload_parts(
            src,
            out_dir,
            FILESTER_MAX_PART_BYTES,
            skip_check=_skip_check,
            delete_source=delete_source,
        )

    last_part: dict | None = None
    try:
        for part in part_source:
            if should_cancel and should_cancel():
                raise TransferCancelled("Upload cancelled")
            part_path = part["path"]
            last_part = part
            if on_log and not part.get("is_source"):
                on_log(
                    f"[Filester] Uploading part {part['part_index']}/{part['part_count']}: "
                    f"{part['filename']} ({format_size(part['size_bytes'])})"
                )
            raw = filester_upload.upload_file(
                part_path,
                folder_id=upload_folder_id,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
            results.append(_normalize_filester(raw, part=part))
            if not part.get("is_source"):
                try:
                    os.remove(part_path)
                except OSError:
                    pass

        if on_log and last_part and last_part.get("part_count", 1) > 1:
            stem, ext = os.path.splitext(
                last_part.get("original_basename") or os.path.basename(src)
            )
            original = f"{stem}{ext}"
            mode = last_part.get("split_mode") or split_mode
            if mode in ("ffmpeg", "ffmpeg_slice"):
                on_log(
                    f"[Filester] Split into {last_part['part_count']} playable parts. "
                    f"Rejoin: printf \"file '%s'\\n\" {stem}.PART*{ext} > parts.txt && "
                    f"ffmpeg -f concat -safe 0 -i parts.txt -c copy {original}"
                )
            else:
                on_log(
                    f"[Filester] Split into {last_part['part_count']} parts. "
                    f"Linux: cat {original}.part* > {original} | "
                    f"Windows: copy /b {original}.part001+...+{original}"
                )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    return results, upload_folder_id


def upload_source(
    path,
    folder_id=None,
    filester_folder_id=None,
    on_progress=None,
    should_cancel=None,
    on_log=None,
    job_id=None,
) -> tuple[list[UploadResult], str | None, str | None]:
    """Upload a file or directory to all enabled/feasible providers.

    ``folder_id`` is the GoFile folder. ``filester_folder_id`` is resolved by
    the caller from the GoFile folder display name when not supplied.

    Returns (results, filester_skip_reason, filester_folder_id_for_url).
    ``filester_folder_id_for_url`` may be a split-upload subfolder id. Raises if
    every planned destination fails. Filester skip is informational when GoFile still runs.
    """
    from downloader import TransferCancelled

    all_results: list[UploadResult] = []
    filester_skip_reason: str | None = None
    filester_url_folder_id: str | None = (filester_folder_id or "").strip() or None

    for src in _expand_sources(path):
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")

        size = os.path.getsize(src)
        destinations, skip = plan_upload_destinations(size)
        if skip and FILESTER_ENABLED and "filester" not in destinations:
            filester_skip_reason = skip
            if on_log:
                on_log(f"[Filester] Skipped: {skip}")

        if not destinations:
            raise RuntimeError(
                filester_skip_reason or "No upload destination available for this file"
            )

        src_results: list[UploadResult] = []
        gofile_ran = "gofile" in destinations
        filester_ran = "filester" in destinations

        if gofile_ran:
            if on_log:
                on_log(f"[GoFile] Uploading {os.path.basename(src)} ({format_size(size)})")
            src_results.append(
                _upload_gofile(
                    src,
                    folder_id=folder_id,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
            )

        if filester_ran:
            fs_folder = (filester_folder_id or "").strip() or None
            fs_results, effective_fs_folder = _upload_filester_parts(
                src,
                folder_id=fs_folder,
                on_progress=on_progress,
                should_cancel=should_cancel,
                on_log=on_log,
                job_id=job_id,
                delete_source=not gofile_ran,
            )
            src_results.extend(fs_results)
            if effective_fs_folder:
                filester_url_folder_id = effective_fs_folder
            if gofile_ran and os.path.isfile(src):
                try:
                    os.remove(src)
                except OSError:
                    pass

        failed = [r for r in src_results if not r.ok]
        succeeded = [r for r in src_results if r.ok]
        if not succeeded:
            detail = failed[0].raw if failed else "unknown"
            raise RuntimeError(f"All upload destinations failed: {detail}")
        if failed and on_log:
            for r in failed:
                on_log(f"[{r.provider}] Upload failed: {r.raw}")

        all_results.extend(src_results)

    return all_results, filester_skip_reason, filester_url_folder_id
