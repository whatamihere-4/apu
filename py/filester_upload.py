"""Filester upload client (https://filester.me/api-docs).

Mirrors the gofile_upload public surface so the two providers are
interchangeable behind upload_provider.py. Switching providers is a pure
env-var change (UPLOAD_PROVIDER=filester) plus FILESTER_API_KEY.
"""
from __future__ import annotations

import os
import re
import time

import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from downloader import TransferCancelled
from upload_common import format_size


FILESTER_API_KEY = os.environ.get("FILESTER_API_KEY", "")
FILESTER_BASE_URL = (os.environ.get("FILESTER_BASE_URL") or "https://u1.filester.me").rstrip("/")
# Public download page host (the API base is an upload node, e.g. u1.filester.me).
FILESTER_SITE_URL = (os.environ.get("FILESTER_SITE_URL") or "https://filester.me").rstrip("/")

_FOLDER_NAME_MAX = 100


def _auth_headers():
    h = {}
    if FILESTER_API_KEY:
        h["Authorization"] = f"Bearer {FILESTER_API_KEY}"
    return h


def _flatten_folder_rows(rows: list, out: dict[str, str]) -> None:
    """Recursively collect {id: name} from Filester folder list payloads."""
    for item in rows:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or item.get("identifier") or "").strip()
        name = str(item.get("name") or "").strip()
        if fid and name:
            out[fid] = name
        for child_key in ("children", "folders", "subfolders"):
            children = item.get(child_key)
            if isinstance(children, list) and children:
                _flatten_folder_rows(children, out)


def sanitize_folder_name(name: str, *, max_len: int = _FOLDER_NAME_MAX) -> str:
    """Filester folder name: strip unsafe chars, collapse whitespace, cap length."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        s = "upload"
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "upload"


def fetch_folder_map_from_api() -> dict[str, str]:
    """Download the account folder map from GET /api/v1/folders."""
    if not FILESTER_API_KEY:
        raise RuntimeError("FILESTER_API_KEY is not set")
    url = f"{FILESTER_BASE_URL}/api/v1/folders"
    r = requests.get(url, headers=_auth_headers(), timeout=60)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"Filester folders API failed: {body}")
    rows = body.get("data")
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected Filester folders response: {body!r}")
    out: dict[str, str] = {}
    _flatten_folder_rows(rows, out)
    return out


def get_root_folder_id():
    """Filester has no root-folder concept; an empty folder id uploads to root."""
    return ""


def create_folder(parent_id, name, *, name_suffix: str | None = None):
    """Create a folder, optionally nested under ``parent_id`` (Filester ``parent`` field)."""
    base = sanitize_folder_name(name)
    if name_suffix:
        suffix = str(name_suffix).strip()
        max_base = _FOLDER_NAME_MAX - len(suffix) - 1
        if max_base < 1:
            folder_name = suffix[:_FOLDER_NAME_MAX]
        else:
            folder_name = f"{base[:max_base].rstrip()}-{suffix}"
    else:
        folder_name = base

    payload: dict = {"name": folder_name, "public": 1}
    pid = (parent_id or "").strip()
    if pid:
        payload["parent"] = pid

    r = requests.post(
        f"{FILESTER_BASE_URL}/api/v1/folder",
        headers=_auth_headers(),
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        msg = str(data.get("message") or data)
        if name_suffix is None and pid and "exist" in msg.lower():
            return create_folder(parent_id, name, name_suffix="2")
        raise RuntimeError(f"Failed to create folder: {data}")
    identifier = (data.get("data") or {}).get("identifier")
    if not identifier:
        raise RuntimeError(f"Filester returned no folder identifier: {data}")
    return identifier


def folder_url(folder_id):
    return f"{FILESTER_SITE_URL}/f/{folder_id}" if folder_id else FILESTER_SITE_URL


def gallery_url_from_response(raw: dict) -> str:
    """Build a public download URL from a Filester upload JSON response."""
    if not isinstance(raw, dict):
        return ""
    url = str(raw.get("url") or "").strip()
    if url:
        return url
    slug = str(raw.get("slug") or "").strip()
    if slug:
        return f"{FILESTER_SITE_URL}/d/{slug}"
    data = raw.get("data")
    if isinstance(data, dict):
        url = str(data.get("url") or "").strip()
        if url:
            return url
        slug = str(data.get("slug") or "").strip()
        if slug:
            return f"{FILESTER_SITE_URL}/d/{slug}"
    return ""


def upload_file(filepath, folder_id=None, on_progress=None, should_cancel=None):
    """Upload a single file to Filester.

    on_progress(pct, uploaded, total, speed, eta_seconds) is called at most
    once per second. should_cancel(): if it returns True, abort (TransferCancelled).
    Returns the raw Filester JSON response.
    """
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    url = f"{FILESTER_BASE_URL}/api/v1/upload"
    print(
        f"[FILESTER] upload {filename} ({format_size(filesize)}) -> {url}",
        flush=True,
    )

    last_log = [0.0]
    start_time = [time.time()]

    def progress_callback(monitor):
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")
        now = time.time()
        if now - last_log[0] < 1.0:
            return
        last_log[0] = now
        elapsed = now - start_time[0]
        uploaded = monitor.bytes_read
        speed = uploaded / elapsed if elapsed > 0 else 0
        pct = (uploaded / monitor.len) * 100 if monitor.len else 0
        remaining = ((monitor.len - uploaded) / speed) if speed > 0 else 0
        print(
            f"[FILESTER] {filename}: {pct:5.1f}%  "
            f"{format_size(uploaded)}/{format_size(monitor.len)}  "
            f"{format_size(speed)}/s  "
            f"ETA {int(remaining)}s",
            flush=True,
        )
        if on_progress:
            on_progress(pct, uploaded, monitor.len, speed, remaining)

    last_err = None
    for attempt in range(1, 4):
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")
        try:
            with open(filepath, "rb") as fp:
                fields = {"file": (filename, fp, "application/octet-stream")}
                encoder = MultipartEncoder(fields=fields)
                monitor = MultipartEncoderMonitor(encoder, progress_callback)
                headers = _auth_headers()
                headers["Content-Type"] = monitor.content_type
                if folder_id:
                    headers["X-Folder-ID"] = folder_id
                r = requests.post(url, data=monitor, headers=headers)

            if r.status_code == 429:
                last_err = RuntimeError("Filester rate limit (429)")
                print(f"[FILESTER] attempt {attempt} rate limited, backing off", flush=True)
                time.sleep(min(5 * attempt, 30))
                continue
            if r.status_code >= 500:
                snippet = (r.text or "")[:300]
                last_err = RuntimeError(f"Filester returned HTTP {r.status_code}: {snippet}")
                print(f"[FILESTER] attempt {attempt} got {r.status_code}, retrying", flush=True)
                time.sleep(min(2 * attempt, 8))
                continue

            r.raise_for_status()
            result = r.json()
            break
        except TransferCancelled:
            raise
        except requests.RequestException as e:
            last_err = e
            print(f"[FILESTER] attempt {attempt} request error: {e}", flush=True)
            time.sleep(min(2 * attempt, 8))
    else:
        raise RuntimeError(f"Upload failed to Filester: {last_err}")

    if on_progress:
        on_progress(100.0, filesize, filesize, 0, 0)

    if result.get("success"):
        dl = gallery_url_from_response(result) or "N/A"
        print(f"[FILESTER] {filename} DONE -> {dl}", flush=True)
    else:
        print(f"[FILESTER] {filename} FAILED: {result}", flush=True)

    return result
