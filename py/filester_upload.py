"""Filester upload client (https://filester.me/api-docs).

Mirrors the gofile_upload public surface so the two providers are
interchangeable behind upload_provider.py. Switching providers is a pure
env-var change (UPLOAD_PROVIDER=filester) plus FILESTER_API_KEY.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse

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


def _flatten_folder_rows(rows: list, out: dict[str, str], *, recurse: bool = True) -> None:
    """Collect {id: name} from Filester folder list payloads."""
    for item in rows:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or item.get("identifier") or "").strip()
        name = str(item.get("name") or "").strip()
        if fid and name:
            out[fid] = name
        if not recurse:
            continue
        for child_key in ("children", "folders", "subfolders"):
            children = item.get(child_key)
            if isinstance(children, list) and children:
                _flatten_folder_rows(children, out, recurse=recurse)


def sanitize_folder_name(name: str, *, max_len: int = _FOLDER_NAME_MAX) -> str:
    """Filester folder name: strip unsafe chars, collapse whitespace, cap length."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        s = "upload"
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "upload"


def fetch_folder_map_from_api(*, include_children: bool = True) -> dict[str, str]:
    """Download the account folder map from GET /api/v1/folders.

    Recurses the full tree by default so nested studio folders (e.g. under a
    root VR container) are included. Callers should filter with
    :func:`load_folder_blacklist` to drop split-upload subfolders and any
    container folders you do not use as upload targets.
    """
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
    _flatten_folder_rows(rows, out, recurse=include_children)
    return out


def _blacklist_file_path() -> str:
    explicit = (os.environ.get("FILESTER_FOLDER_BLACKLIST_FILE") or "").strip()
    if explicit:
        return explicit
    cache = (os.environ.get("CACHE_DIR") or "").strip()
    if cache:
        return os.path.join(cache, "filester-folder-blacklist.json")
    return ""


def load_folder_blacklist() -> set[str]:
    """Folder ids excluded from filester-folders.json (split subfolders, containers, etc.)."""
    path = _blacklist_file_path()
    if not path:
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x).strip() for x in data if str(x).strip()}
        if isinstance(data, dict):
            ids = data.get("folder_ids") or data.get("ids") or []
            if isinstance(ids, list):
                return {str(x).strip() for x in ids if str(x).strip()}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return set()


def load_upload_subfolder_blacklist() -> set[str]:
    """Backward-compatible alias for :func:`load_folder_blacklist`."""
    return load_folder_blacklist()


def apply_folder_blacklist(folders: dict[str, str]) -> dict[str, str]:
    """Return *folders* minus ids listed in the blacklist JSON."""
    blacklist = load_folder_blacklist()
    if not blacklist:
        return folders
    return {k: v for k, v in folders.items() if k not in blacklist}


def record_upload_subfolder(folder_id: str, *, label: str = "") -> None:
    """Append a split-upload subfolder id to the blacklist JSON."""
    fid = (folder_id or "").strip()
    path = _blacklist_file_path()
    if not fid or not path:
        return
    current_ids = load_folder_blacklist()
    if fid in current_ids:
        return
    current_ids.add(fid)

    notes: dict[str, str] = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                raw_notes = existing.get("notes") or existing.get("labels") or {}
                if isinstance(raw_notes, dict):
                    notes = {str(k): str(v) for k, v in raw_notes.items()}
        except (json.JSONDecodeError, OSError):
            notes = {}

    if label:
        notes[fid] = label
    elif fid not in notes:
        notes[fid] = "split upload subfolder (auto)"

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload: dict = {"folder_ids": sorted(current_ids)}
    if notes:
        payload["notes"] = {k: notes[k] for k in sorted(current_ids) if k in notes}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def remove_folder_from_blacklist(folder_id: str) -> None:
    """Drop a folder id from the blacklist JSON (e.g. after temp folder delete)."""
    fid = (folder_id or "").strip()
    path = _blacklist_file_path()
    if not fid or not path or not os.path.isfile(path):
        return
    current_ids = load_folder_blacklist()
    if fid not in current_ids:
        return
    current_ids.discard(fid)

    notes: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        if isinstance(existing, dict):
            raw_notes = existing.get("notes") or existing.get("labels") or {}
            if isinstance(raw_notes, dict):
                notes = {str(k): str(v) for k, v in raw_notes.items()}
    except (json.JSONDecodeError, OSError):
        notes = {}
    notes.pop(fid, None)

    payload: dict = {"folder_ids": sorted(current_ids)}
    if notes:
        payload["notes"] = {k: notes[k] for k in sorted(current_ids) if k in notes}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


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


def file_identifier_from_response(raw: dict) -> str:
    """Return slug, numeric id, or uuid from a Filester upload JSON body."""
    if not isinstance(raw, dict):
        return ""
    slug = str(raw.get("slug") or "").strip()
    if slug:
        return slug
    file_id = raw.get("file_id")
    if file_id is not None and str(file_id).strip():
        return str(file_id).strip()
    data = raw.get("data")
    if isinstance(data, dict):
        slug = str(data.get("slug") or "").strip()
        if slug:
            return slug
        fid = data.get("id")
        if fid is not None and str(fid).strip():
            return str(fid).strip()
        uuid_val = str(data.get("uuid") or "").strip()
        if uuid_val:
            return uuid_val
    return ""


def move_files(file_identifiers: list[str], folder_id: str) -> dict:
    """Move files into ``folder_id`` via POST /api/v1/files/move (bulk)."""
    ids = [str(x).strip() for x in file_identifiers if str(x).strip()]
    if not ids:
        raise ValueError("no file identifiers to move")
    dest = (folder_id or "").strip()
    if not dest:
        raise ValueError("destination folder id required")

    payload = {"files": ids, "folder": dest}
    r = requests.post(
        f"{FILESTER_BASE_URL}/api/v1/files/move",
        headers={**_auth_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"Filester move failed: {body}")
    return body.get("data") if isinstance(body.get("data"), dict) else body


def list_folder_files(folder_id: str) -> list[dict]:
    """List files in a folder via GET /api/v1/folder/{identifier}/files."""
    fid = (folder_id or "").strip()
    if not fid:
        return []
    url = f"{FILESTER_BASE_URL}/api/v1/folder/{urllib.parse.quote(fid, safe='')}/files"
    r = requests.get(url, headers=_auth_headers(), timeout=60)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        return []
    data = body.get("data")
    return data if isinstance(data, list) else []


def _folder_delete_success(body: dict) -> bool:
    if not isinstance(body, dict):
        return False
    if body.get("success") is True:
        return True
    deleted = body.get("successful_deletes")
    if deleted is not None and int(deleted) >= 1:
        return True
    return False


def delete_folder(folder_id: str) -> dict:
    """Delete one folder, trying documented and v1 API path variants."""
    fid = (folder_id or "").strip()
    if not fid:
        raise ValueError("folder id required")

    headers = {**_auth_headers(), "Content-Type": "application/json"}
    attempts: list[tuple[str, str, dict | None]] = [
        ("POST", f"{FILESTER_BASE_URL}/api/v1/folder/delete", {"identifiers": [fid]}),
        ("POST", f"{FILESTER_BASE_URL}/api/v1/folder/delete", {"folder": fid}),
        ("POST", f"{FILESTER_BASE_URL}/api/v1/folders/delete", {"identifiers": [fid]}),
        ("POST", f"{FILESTER_BASE_URL}/api/v1/folders/delete", {"folders": [fid]}),
        ("POST", f"{FILESTER_BASE_URL}/folder/delete", {"identifiers": [fid]}),
        ("POST", f"{FILESTER_BASE_URL}/api/v1/folder/{urllib.parse.quote(fid, safe='')}/delete", {}),
        ("DELETE", f"{FILESTER_BASE_URL}/api/v1/folder/{urllib.parse.quote(fid, safe='')}", None),
    ]

    errors: list[str] = []
    for method, url, payload in attempts:
        try:
            if method == "DELETE":
                r = requests.delete(url, headers=_auth_headers(), timeout=60)
            else:
                r = requests.post(url, headers=headers, json=payload, timeout=60)
            if r.status_code == 404:
                return {"success": True, "message": "folder already gone", "url": url}
            if r.status_code >= 400:
                snippet = (r.text or "")[:240]
                errors.append(f"{method} {url} -> HTTP {r.status_code}: {snippet}")
                continue
            try:
                body = r.json()
            except ValueError:
                errors.append(f"{method} {url} -> non-JSON response")
                continue
            if _folder_delete_success(body):
                return body
            errors.append(f"{method} {url} -> {body!r}")
        except requests.RequestException as e:
            errors.append(f"{method} {url} -> {e}")

    raise RuntimeError(
        f"Filester folder delete failed for {fid!r}; tried {len(attempts)} variants. "
        f"Last: {errors[-1] if errors else 'unknown'}"
    )


def delete_folders(folder_identifiers: list[str]) -> dict:
    """Delete folders one at a time (API bulk shape varies by deployment)."""
    ids = [str(x).strip() for x in folder_identifiers if str(x).strip()]
    if not ids:
        raise ValueError("no folder identifiers to delete")
    last: dict = {}
    for fid in ids:
        last = delete_folder(fid)
    return last


def delete_empty_folder(folder_id: str, *, on_log=None) -> bool:
    """Delete a folder after confirming it has no files."""
    fid = (folder_id or "").strip()
    if not fid:
        return False
    remaining = list_folder_files(fid)
    if remaining:
        msg = f"folder {fid} still has {len(remaining)} file(s); skipping delete"
        print(f"[FILESTER] {msg}", flush=True)
        if on_log:
            on_log(f"[Filester] Temp folder cleanup skipped: {msg}")
        return False
    delete_folder(fid)
    remove_folder_from_blacklist(fid)
    print(f"[FILESTER] deleted empty folder {fid}", flush=True)
    return True


def rename_split_upload_folder_for_stashdb(
    *,
    parent_folder_id: str,
    temp_folder_id: str,
    scene_title: str,
    upload_responses: list[dict],
    on_log=None,
) -> str:
    """Create a StashDB-titled folder, move split parts, delete the temp folder.

    Returns the folder id parts ended up in (scene folder on success, else temp).
    """
    parent = (parent_folder_id or "").strip()
    temp = (temp_folder_id or "").strip()
    title = sanitize_folder_name(scene_title)
    if not parent or not temp or temp == parent or not title:
        return temp or parent

    file_ids = []
    for raw in upload_responses:
        fid = file_identifier_from_response(raw)
        if fid:
            file_ids.append(fid)
    if not file_ids:
        if on_log:
            on_log("[Filester] StashDB folder rename skipped: no file ids in upload responses")
        return temp

    try:
        scene_folder_id = create_folder(parent, title)
        move_data = move_files(file_ids, scene_folder_id)
        moved = int(move_data.get("moved") or 0)
        failed = int(move_data.get("failed") or 0)
        if failed or moved < len(file_ids):
            if on_log:
                on_log(
                    f"[Filester] StashDB folder move incomplete "
                    f"({moved}/{len(file_ids)} moved, {failed} failed); keeping temp folder"
                )
            return temp
        try:
            if delete_empty_folder(temp, on_log=on_log):
                if on_log:
                    on_log("[Filester] Removed empty temp folder after StashDB rename")
        except Exception as e:  # noqa: BLE001
            if on_log:
                on_log(f"[Filester] Temp split folder delete failed ({e}); parts are in scene folder")
            print(f"[FILESTER] temp folder delete failed: {e}", flush=True)
        record_upload_subfolder(
            scene_folder_id,
            label=f"split upload: {title} (StashDB)",
        )
        if on_log:
            on_log(
                f'[Filester] Moved {moved} part(s) to StashDB folder "{title}" '
                f"({folder_url(scene_folder_id)})"
            )
        return scene_folder_id
    except Exception as e:  # noqa: BLE001
        if on_log:
            on_log(f"[Filester] StashDB folder rename failed ({e}); kept filename folder")
        print(f"[FILESTER] StashDB folder rename failed: {e}", flush=True)
        return temp
