"""Upload provider factory: switch backends with the UPLOAD_PROVIDER env var.

UPLOAD_PROVIDER=gofile (default) or filester. app.py imports the module-level
functions below and stays provider-agnostic. Oversized files are transparently
split into watchable parts before upload (filester only); the job queue,
progress callbacks, and sidecars are unchanged.
"""
from __future__ import annotations

import os
import shutil
import uuid

import requests

import file_splitter
from upload_common import UploadResult, format_size  # noqa: F401 (re-exported)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


UPLOAD_PROVIDER = (os.environ.get("UPLOAD_PROVIDER") or "gofile").strip().lower()

SPLITTER_HTTP_URL = (os.environ.get("SPLITTER_HTTP_URL") or "").strip().rstrip("/")
SPLITTER_HTTP_TOKEN = (os.environ.get("SPLITTER_HTTP_TOKEN") or "").strip()
SPLITTER_HTTP_TIMEOUT = _env_int("SPLITTER_HTTP_TIMEOUT_SEC", 7800)


if UPLOAD_PROVIDER == "filester":
    import filester_upload as _backend

    PROVIDER_LABEL = "Filester"
    MAX_PART_BYTES = _env_int("FILESTER_MAX_PART_BYTES", 9_500_000_000)

    def _normalize(raw: dict) -> UploadResult:
        ok = bool(raw.get("success"))
        return UploadResult(ok=ok, gallery_url=raw.get("url", "") if ok else "", raw=raw)

else:
    import gofile_upload as _backend

    PROVIDER_LABEL = "GoFile"
    MAX_PART_BYTES = None  # GoFile accepts large files; no splitting

    def _normalize(raw: dict) -> UploadResult:
        ok = raw.get("status") == "ok"
        url = (raw.get("data") or {}).get("downloadPage", "") if ok else ""
        return UploadResult(ok=ok, gallery_url=url, raw=raw)


def get_root_folder_id():
    return _backend.get_root_folder_id()


def create_folder(parent_id, name):
    return _backend.create_folder(parent_id, name)


def folder_url(folder_id):
    return _backend.folder_url(folder_id)


def _split_via_http(src: str, max_bytes: int, output_dir: str) -> list[str]:
    headers = {}
    if SPLITTER_HTTP_TOKEN:
        headers["Authorization"] = f"Bearer {SPLITTER_HTTP_TOKEN}"
    r = requests.post(
        f"{SPLITTER_HTTP_URL}/v1/split",
        json={"path": src, "max_bytes": max_bytes, "output_dir": output_dir},
        headers=headers,
        timeout=(10, SPLITTER_HTTP_TIMEOUT),
    )
    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("error", "")
        except ValueError:
            detail = (r.text or "")[:300]
        raise RuntimeError(f"splitter-http returned {r.status_code}: {detail}")
    return r.json().get("parts", [])


def _maybe_split(src, *, job_id, on_log, should_cancel):
    """Return (parts, split_dir). split_dir is None when no split happened."""
    if not MAX_PART_BYTES or os.path.getsize(src) <= MAX_PART_BYTES:
        return [src], None

    token = job_id or uuid.uuid4().hex[:8]
    out_dir = os.path.join(os.path.dirname(src) or ".", f".split_{token}")
    size = os.path.getsize(src)
    if on_log:
        on_log(
            f"{os.path.basename(src)} is {format_size(size)} (> {format_size(MAX_PART_BYTES)}); "
            f"splitting into parts (ffmpeg stream copy, no re-encode)"
        )
    if SPLITTER_HTTP_URL:
        parts = _split_via_http(src, MAX_PART_BYTES, out_dir)
    else:
        parts = file_splitter.split_file(
            src, MAX_PART_BYTES, out_dir, on_log=on_log, should_cancel=should_cancel
        )
    if on_log and len(parts) > 1:
        base = os.path.basename(src)
        names = " ".join(os.path.basename(p) for p in parts)
        # concat demuxer (list file) is the reliable lossless rejoin for mp4/mkv
        # stream copies; the `concat:` protocol only works for raw TS streams.
        on_log(
            f"Split into {len(parts)} parts. Rejoin losslessly with: "
            f'printf "file \'%s\'\\n" {names} > parts.txt && '
            f"ffmpeg -f concat -safe 0 -i parts.txt -c copy {base}"
        )
    return parts, out_dir


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


def upload_source(
    path,
    folder_id=None,
    on_progress=None,
    should_cancel=None,
    on_log=None,
    job_id=None,
) -> list[UploadResult]:
    """Upload a file or directory, splitting oversized files first.

    Returns a normalized UploadResult per uploaded (possibly split) file.
    """
    from downloader import TransferCancelled

    results: list[UploadResult] = []
    split_dirs: list[str] = []
    try:
        for src in _expand_sources(path):
            if should_cancel and should_cancel():
                raise TransferCancelled("Upload cancelled")
            parts, split_dir = _maybe_split(
                src, job_id=job_id, on_log=on_log, should_cancel=should_cancel
            )
            if split_dir:
                split_dirs.append(split_dir)
            for part in parts:
                if should_cancel and should_cancel():
                    raise TransferCancelled("Upload cancelled")
                raw = _backend.upload_file(
                    part,
                    folder_id=folder_id,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
                results.append(_normalize(raw))
    finally:
        for d in split_dirs:
            shutil.rmtree(d, ignore_errors=True)
    return results
