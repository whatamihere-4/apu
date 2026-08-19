import os
import posixpath
import sys
import json
import uuid
import queue
import threading
import mimetypes
import shutil
import subprocess
import tempfile
import time
import re
import hashlib
import urllib.parse
from datetime import datetime
from io import BytesIO

import requests
from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context, redirect

# Keep helper modules under ./py while app.py stays at repo root.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PY_DIR = os.path.join(APP_DIR, "py")
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from upload_provider import (
    upload_source,
    format_size,
    create_folder,
    get_root_folder_id,
    folder_url,
    UPLOAD_PROVIDER,
    PROVIDER_LABEL,
    GOFILE_ENABLED,
    FILESTER_ENABLED,
    ACTIVE_PROVIDERS,
    FILESTER_SPLIT_MODE,
    FILESTER_SPLIT_FALLBACK,
    FILESTER_MAX_PART_BYTES,
    resolve_split_mode,
)
from upload_progress import UploadProgressReporter
from downloader import download_file, TransferCancelled
from filester_upload import (
    apply_folder_blacklist,
    fetch_folder_map_from_api,
    organize_split_parts_into_folder,
    prepare_split_scene_folder,
    try_set_split_folder_thumbnail,
)
from oshash_remote import fetch_oshash_from_url
from gif_host import gif_encode_limits, resolved_gif_host
from queue_persist import track_add, track_remove
from queue_api import register_queue_routes
from upload_resume import (
    cleanup_split_artifacts,
    delete_upload_resume_state,
    load_interrupted_job,
    clear_interrupted_job,
    resume_job_dir,
)
from upload_watchdog import run_watchdog_once, watchdog_settings
import server_scan

app = Flask(__name__)


def _env_int(name, default):
    value = os.environ.get(name, str(default))
    try:
        return int(value)
    except (TypeError, ValueError):
        print(f"[CONFIG] Invalid {name}={value!r}; using {default}", flush=True)
        return default


def _env_yes(name: str, *, default: str = "1") -> bool:
    """True unless value is 0 / false / no / off (same convention as THUMBER_ENABLED)."""
    raw = (os.environ.get(name) or default).strip().lower()
    return raw not in ("0", "false", "no", "off", "")


DOWNLOADS_DIR = "/downloads"


def _downloads_mount_rel(full_path: str) -> str:
    """Path of ``full_path`` relative to ``DOWNLOADS_DIR``, using ``/`` (for APIs + scenes.json)."""
    if not full_path or not isinstance(full_path, str):
        raise ValueError("path required")
    try:
        root = os.path.realpath(DOWNLOADS_DIR)
        full = os.path.realpath(full_path)
    except OSError:
        root = os.path.abspath(DOWNLOADS_DIR)
        full = os.path.abspath(full_path)
    try:
        rel = os.path.relpath(full, root)
    except ValueError as e:
        raise ValueError("file is not under the downloads mount") from e
    norm = os.path.normpath(rel)
    if norm.startswith(".." + os.sep) or norm == "..":
        raise ValueError("file is not under the downloads mount")
    return norm.replace(os.sep, "/")


def _downloads_key_for_sidecars(full_path: str) -> str:
    """Mount-relative key for hasher/thumber/StashDB; basename if outside ``DOWNLOADS_DIR``."""
    try:
        return _downloads_mount_rel(full_path)
    except ValueError:
        return os.path.basename(full_path)


# Same host path as thumber's THUMBER_THUMBS → /thumbs (see docker-compose bind).
THUMBS_DIR = os.path.realpath(
    (os.environ.get("THUMBS_DIR") or "/thumbs").rstrip("/") or "/thumbs"
)


def _resolve_cache_dir(path: str) -> str:
    """Resolve cache dir; relative paths are anchored to APP_DIR, not process cwd."""
    path = (path or "").strip() or os.path.join(APP_DIR, "cache")
    if not os.path.isabs(path):
        path = os.path.join(APP_DIR, path)
    return path


HASHES_DIR = os.path.realpath(
    _resolve_cache_dir(
        (os.environ.get("CACHE_DIR") or os.path.join(APP_DIR, "cache")).rstrip("/")
        or os.path.join(APP_DIR, "cache")
    )
)
GOFILE_FOLDERS_FILE = (
    (os.environ.get("GOFILE_FOLDERS_FILE") or "").strip()
    or os.path.join(HASHES_DIR, "gofile-folders.json")
)
_LEGACY_GOFILE_FOLDERS_FILE = os.path.join(HASHES_DIR, "folders.json")
FILESTER_FOLDERS_FILE = (
    (os.environ.get("FILESTER_FOLDERS_FILE") or "").strip()
    or os.path.join(HASHES_DIR, "filester-folders.json")
)
SCENES_FILE = os.path.join(HASHES_DIR, "scenes.json")
_LEGACY_HASHES_FILE = os.path.join(HASHES_DIR, "hashes.json")
PERFORMERS_FILE = os.path.join(HASHES_DIR, "performers.json")
STUDIO_ALIASES_FILE = os.path.join(HASHES_DIR, "studio_aliases.json")
STUDIO_THREADS_FILE = os.path.join(HASHES_DIR, "studio_threads.json")
RESOLUTION_PRESETS_FILE = os.path.join(HASHES_DIR, "resolution_presets.json")
THUMBER_COMPOSE_FILE = os.environ.get(
    "THUMBER_COMPOSE_FILE", "/app/docker-compose.yml"
)
THUMBER_PROJECT_DIR = os.environ.get(
    "THUMBER_PROJECT_DIR", "/app"
)
THUMBER_SERVICE = os.environ.get("THUMBER_SERVICE", "thumber")
THUMBER_LOG_LIMIT = _env_int("THUMBER_LOG_LIMIT", 80)
THUMBER_DOCKER_BIN = (os.environ.get("THUMBER_DOCKER_BIN") or "").strip()
THUMBER_ENABLED = os.environ.get("THUMBER_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
# If <stem>_thumbs.png exists under THUMBS_DIR for this basename, skip running thumber/ffmpeg.
THUMBER_USE_CACHED_THUMBS = os.environ.get("THUMBER_USE_CACHED_THUMBS", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
THUMBER_HTTP_URL = (os.environ.get("THUMBER_HTTP_URL") or "").strip()
THUMBER_HTTP_TIMEOUT = _env_int("THUMBER_HTTP_TIMEOUT", 7200)
THUMBER_HTTP_BEARER = (os.environ.get("THUMBER_HTTP_BEARER") or "").strip()
THUMBER_HTTP_TOKEN = (os.environ.get("THUMBER_HTTP_TOKEN") or "").strip()
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"
}
STASHDB_API_KEY = (os.environ.get("STASHDB_API_KEY") or "").strip()
STASHDB_GRAPHQL_URL = (os.environ.get("STASHDB_GRAPHQL_URL") or "https://stashdb.org/graphql").strip()
# hasher-http sidecar: computes OSHASH / PHASH via /v1/hash. Aliases of the
# old PHASHER_* env names are still accepted to preserve existing deploys.
HASHER_HTTP_URL = (
    os.environ.get("HASHER_HTTP_URL")
    or os.environ.get("PHASHER_HTTP_URL")
    or ""
).strip().rstrip("/")
HASHER_HTTP_TOKEN = (
    os.environ.get("HASHER_HTTP_TOKEN")
    or os.environ.get("PHASHER_HTTP_TOKEN")
    or ""
).strip()
HASHER_HTTP_TIMEOUT = _env_int(
    "HASHER_HTTP_TIMEOUT_SEC",
    _env_int("PHASHER_HTTP_TIMEOUT_SEC", 1800),
)
# Master switch for hasher-http usage in apu. When 0, HASHER_*_ENABLED for individual algos are ignored.
HASHER_ENABLED = _env_yes("HASHER_ENABLED", default="1")
# Per-algorithm (only if HASHER_ENABLED and HASHER_HTTP_URL are both on).
HASHER_OSHASH_ENABLED = _env_yes("HASHER_OSHASH_ENABLED", default="1")
HASHER_PHASH_ENABLED = _env_yes("HASHER_PHASH_ENABLED", default="1")
# When 1, only compute PHASH after a StashDB match when OSHASH did not match.
# The upload check already short-circuits OSHASH → PHASH; this also skips the
# post-match PHASH follow-up (submitFingerprint) when the match came from OSHASH.
HASHER_PHASH_ONLY_ON_MISS = _env_yes("HASHER_PHASH_ONLY_ON_MISS", default="0")
STASHDB_DRAFTS_BASE = (os.environ.get("STASHDB_DRAFTS_BASE") or "https://stashdb.org/drafts").rstrip("/")
STASHDB_SCENES_BASE = (os.environ.get("STASHDB_SCENES_BASE") or "https://stashdb.org/scenes").rstrip("/")
# After a StashDB match during post-upload check, submit fingerprints via submitFingerprint (kill-switch).
STASHDB_AUTO_CONTRIBUTE = os.environ.get("STASHDB_AUTO_CONTRIBUTE", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# Optional background sync of cache/filester-folders.json from GET /api/v1/folders.
FILESTER_FOLDER_SYNC_ENABLED = _env_yes("FILESTER_FOLDER_SYNC_ENABLED", default="0")
FILESTER_FOLDER_SYNC_INTERVAL_SEC = max(
    60,
    _env_int("FILESTER_FOLDER_SYNC_INTERVAL_SEC", 3600),
)
FILESTER_FOLDER_SYNC_INCLUDE_CHILDREN = _env_yes(
    "FILESTER_FOLDER_SYNC_INCLUDE_CHILDREN", default="1"
)


FILESTER_MERGE_PARTS_GUIDE_URL = (
    os.environ.get("FILESTER_MERGE_PARTS_GUIDE_URL")
    or "https://filester.me/f/009635c93a89316f"
).strip()
FILESTER_MERGE_PARTS_GUIDE_SIZE = max(1, min(_env_int("FILESTER_MERGE_PARTS_GUIDE_SIZE", 3), 7))

GOONBOX_BASE_URL = (os.environ.get("GOONBOX_BASE_URL") or "https://goonbox.cr").rstrip("/")
GIFYU_BASE_URL = (os.environ.get("GIFYU_BASE_URL") or "https://gifyu.com").rstrip("/")
PIXHOST_API_URL = (os.environ.get("PIXHOST_API_URL") or "https://api.pixhost.cc").rstrip("/")
GIF_HOST = resolved_gif_host()
GIF_ENCODE_LIMITS = gif_encode_limits(GIF_HOST)
JOB_LINKS_FILENAME_ONLY = _env_yes("JOB_LINKS_FILENAME_ONLY", default="0")
HASHER_ALGORITHMS = ("OSHASH", "PHASH")


def _hasher_sidecar_configured() -> bool:
    return bool(HASHER_HTTP_URL)


def _hasher_service_active() -> bool:
    return HASHER_ENABLED and _hasher_sidecar_configured()


def _hasher_algo_enabled(algo: str) -> bool:
    """True if this algorithm may call hasher-http (HASHER_ENABLED and per-algo flags)."""
    if not HASHER_ENABLED or not _hasher_sidecar_configured():
        return False
    a = (algo or "").strip().upper()
    if a == "OSHASH":
        return HASHER_OSHASH_ENABLED
    if a == "PHASH":
        return HASHER_PHASH_ENABLED
    return False


def _helper_stashdb_lookup_algos() -> tuple[str, ...]:
    return tuple(x for x in ("OSHASH", "PHASH") if _hasher_algo_enabled(x))


jobs = {}
_cancelled = set()
_job_queue = queue.Queue()


def _is_cancelled(job_id):
    return job_id in _cancelled


def _queue_worker():
    while True:
        job_id, func = _job_queue.get()
        if _is_cancelled(job_id):
            jobs[job_id]["status"] = "cancelled"
            jobs[job_id]["status_text"] = "Cancelled"
            jobs[job_id]["progress"] = None
            _job_queue.task_done()
            continue
        try:
            func()
        except Exception as e:
            if not _is_cancelled(job_id):
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["progress"] = None
                print(f"[ERROR] Job {job_id}: {e}", flush=True)
        finally:
            _job_queue.task_done()


threading.Thread(target=_queue_worker, daemon=True).start()


def _enqueue(job_id, func):
    pending = _job_queue.qsize()
    if pending > 0:
        jobs[job_id]["status_text"] = f"Queued — #{pending + 1} in line"
    _job_queue.put((job_id, func))


# ── Folder registry (file-backed) ───────────────────────────────────

_gofile_root_id = None


def _load_gofile_folders():
    """GoFile folder map from cache/gofile-folders.json."""
    for path in (GOFILE_FOLDERS_FILE, _LEGACY_GOFILE_FOLDERS_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {}


def _filester_split_job_accessors(job_id: str):
    """Callbacks so Filester split upload can read/update per-job scene folder state."""

    def get_split_meta() -> dict:
        job = jobs.get(job_id) or {}
        match = job.get("stashdb_match") or {}
        return {
            "dest_folder_id": (job.get("filester_split_dest_folder_id") or "").strip() or None,
            "scene_title": (match.get("title") or "").strip(),
            "cover_path": job.get("stashdb_cover_path"),
        }

    def set_split_dest_folder_id(folder_id: str) -> None:
        job = jobs.get(job_id)
        if job is not None:
            job["filester_split_dest_folder_id"] = folder_id

    return get_split_meta, set_split_dest_folder_id


_filester_split_folder_locks: dict[str, threading.Lock] = {}
_filester_split_folder_locks_guard = threading.Lock()


def _filester_split_folder_lock(job_id: str) -> threading.Lock:
    with _filester_split_folder_locks_guard:
        if job_id not in _filester_split_folder_locks:
            _filester_split_folder_locks[job_id] = threading.Lock()
        return _filester_split_folder_locks[job_id]


def _maybe_prepare_filester_split_folder(job_id: str) -> None:
    """When StashDB matches during an in-flight split upload, create the scene folder early."""
    job = jobs.get(job_id)
    if not job or not job.get("filester_split_active"):
        return
    if job.get("status") != "uploading":
        return
    studio = (job.get("filester_folder_id") or "").strip()
    if not studio or (job.get("filester_split_dest_folder_id") or "").strip():
        return
    match = job.get("stashdb_match") or {}
    scene_title = (match.get("title") or "").strip()
    if not scene_title:
        return
    source = (job.get("source_filename") or "upload").strip()
    stem, _ext = os.path.splitext(source)
    log_fn = lambda ln: _append_job_log(job_id, ln)
    with _filester_split_folder_lock(job_id):
        job = jobs.get(job_id)
        if not job or (job.get("filester_split_dest_folder_id") or "").strip():
            return
        try:
            dest = prepare_split_scene_folder(
                parent_folder_id=studio,
                folder_name=stem or "upload",
                folder_title=scene_title,
                cover_image_path=job.get("stashdb_cover_path"),
                blacklist_label=f"split upload: {scene_title} (StashDB)",
                on_log=log_fn,
            )
            job["filester_split_dest_folder_id"] = dest
        except Exception as exc:  # noqa: BLE001
            log_fn(
                f"[Filester] Early scene folder create failed (will retry after part 1): {exc}"
            )


def _load_filester_folders():
    """Filester folder map from cache/filester-folders.json."""
    try:
        with open(FILESTER_FOLDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _load_folders():
    """Backward-compatible alias: GoFile folder map."""
    return _load_gofile_folders()


def _save_folders(folders):
    """Write GoFile folder map to cache/gofile-folders.json."""
    os.makedirs(HASHES_DIR, exist_ok=True)
    with open(GOFILE_FOLDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(folders, f, indent=2, sort_keys=True)
    print(f"[FOLDERS] Saved {len(folders)} GoFile folder(s) to {GOFILE_FOLDERS_FILE}", flush=True)


def _save_filester_folders(folders):
    """Write Filester folder map to cache/filester-folders.json."""
    os.makedirs(HASHES_DIR, exist_ok=True)
    with open(FILESTER_FOLDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(folders, f, indent=2, sort_keys=True)
    print(
        f"[FOLDERS] Saved {len(folders)} Filester folder(s) to {FILESTER_FOLDERS_FILE}",
        flush=True,
    )


def _sync_filester_folders_from_api(*, mode: str = "replace") -> dict:
    """Pull folders from Filester API into filester-folders.json.

    mode: ``replace`` (API is source of truth) or ``merge`` (keep local-only ids).
    """
    remote = fetch_folder_map_from_api(
        include_children=FILESTER_FOLDER_SYNC_INCLUDE_CHILDREN
    )
    remote = apply_folder_blacklist(remote)
    if mode == "merge":
        merged = dict(_load_filester_folders())
        merged.update(remote)
        folders = merged
    elif mode == "replace":
        folders = remote
    else:
        raise ValueError(f"Unknown sync mode: {mode!r}")
    _save_filester_folders(folders)
    return {
        "mode": mode,
        "count": len(folders),
        "from_api": len(remote),
    }


_filester_sync_lock = threading.Lock()
_filester_sync_state: dict = {
    "last_at": None,
    "last_count": 0,
    "last_error": None,
}


def _run_filester_folder_sync_once() -> bool:
    """Sync Filester folders if enabled. Returns True when a sync ran."""
    if not (FILESTER_ENABLED and FILESTER_FOLDER_SYNC_ENABLED):
        return False
    if not _filester_sync_lock.acquire(blocking=False):
        return False
    try:
        summary = _sync_filester_folders_from_api(mode="replace")
        _filester_sync_state["last_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        _filester_sync_state["last_count"] = int(summary.get("count") or 0)
        _filester_sync_state["last_error"] = None
        print(
            f"[FOLDERS] Filester background sync: {summary.get('count', 0)} folder(s)",
            flush=True,
        )
        return True
    except Exception as exc:
        _filester_sync_state["last_error"] = str(exc)
        print(f"[FOLDERS] Filester background sync failed: {exc}", flush=True)
        return False
    finally:
        _filester_sync_lock.release()


def _filester_folder_sync_loop() -> None:
    _run_filester_folder_sync_once()
    while True:
        time.sleep(FILESTER_FOLDER_SYNC_INTERVAL_SEC)
        _run_filester_folder_sync_once()


def _start_filester_folder_sync_background() -> None:
    if not (FILESTER_ENABLED and FILESTER_FOLDER_SYNC_ENABLED):
        return
    print(
        f"[FOLDERS] Filester background sync enabled "
        f"(every {FILESTER_FOLDER_SYNC_INTERVAL_SEC}s)",
        flush=True,
    )
    threading.Thread(
        target=_filester_folder_sync_loop,
        daemon=True,
        name="filester-folder-sync",
    ).start()


def _parse_gofile_folder_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        path = urllib.parse.urlparse(raw).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() == "d":
            return parts[1]
        if parts:
            return parts[-1]
    return raw.rstrip("/").split("/")[-1]


def _parse_filester_folder_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        path = urllib.parse.urlparse(raw).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() in ("f", "folder"):
            return parts[1]
        if parts:
            return parts[-1]
    return raw.rstrip("/").split("/")[-1]


def _folder_match_key(label: str) -> str:
    """Case-insensitive key with all whitespace removed (for fuzzy folder match)."""
    return re.sub(r"\s+", "", str(label or "").strip().casefold())


def _folder_labels_by_match_key(folders: dict) -> dict[str, str]:
    """Map normalized label key → display label (last wins on duplicates)."""
    out: dict[str, str] = {}
    for _folder_id, label in folders.items():
        if not isinstance(label, str):
            continue
        key = _folder_match_key(label)
        if key:
            out[key] = label.strip()
    return out


def _folder_parity_report() -> dict:
    """Compare GoFile and Filester folder maps by normalized display name."""
    gofile = _folder_labels_by_match_key(_load_gofile_folders())
    filester = _folder_labels_by_match_key(_load_filester_folders())
    gofile_keys = set(gofile)
    filester_keys = set(filester)
    only_gofile = sorted(
        (gofile[k] for k in gofile_keys - filester_keys),
        key=str.casefold,
    )
    only_filester = sorted(
        (filester[k] for k in filester_keys - gofile_keys),
        key=str.casefold,
    )
    return {
        "only_gofile": only_gofile,
        "only_filester": only_filester,
        "matched_count": len(gofile_keys & filester_keys),
        "gofile_count": len(gofile),
        "filester_count": len(filester),
    }


_NETWORK_SUFFIX_RE = re.compile(r"\s*\(Network\)\s*$", re.IGNORECASE)
# Substrings that must keep literal "VR" when matching folder names (brand puns).
_VR_PUN_SUBSTRINGS = ("povr", "pervrt", "vroomed")

_STUDIO_GRAPHQL_FRAGMENT = """
  name
  parent { id name }
"""


def _strip_network_suffix(name: str) -> str:
    """Strip StashDB network suffix, e.g. ``Naughty America (Network)`` → ``Naughty America``."""
    return _NETWORK_SUFFIX_RE.sub("", str(name or "").strip()).strip()


def _stashdb_studio_names(studio_obj) -> tuple[str, str | None]:
    """Return (substudio_name, parent_network_name_or_None) from a StashDB studio fragment."""
    if not isinstance(studio_obj, dict):
        return "", None
    substudio = (studio_obj.get("name") or "").strip()
    parent = studio_obj.get("parent")
    network = None
    if isinstance(parent, dict):
        raw = (parent.get("name") or "").strip()
        if raw:
            network = raw
    return substudio, network


def _parse_studio_display_label(label: str) -> tuple[str, str | None]:
    """Split a ``Substudio / Network`` display label into its parts."""
    s = str(label or "").strip()
    if " / " not in s:
        return s, None
    left, right = s.rsplit(" / ", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        return s, None
    return left, _strip_network_suffix(right)


def _should_strip_vr_from_label(label: str) -> bool:
    cf = str(label or "").casefold()
    # Brand names like KinkVR / BaDoinkVR — keep literal VR for folder lookup.
    if cf.endswith("vr"):
        return False
    return not any(p in cf for p in _VR_PUN_SUBSTRINGS)


def _strip_vr_from_label(label: str) -> str:
    """Remove capital ``VR`` substrings unless the label is a pun brand (POVR, perVRt, VRoomed)."""
    s = str(label or "").strip()
    if not s or not _should_strip_vr_from_label(s):
        return s
    return re.sub(r"VR", "", s).strip()


def _folder_id_search(
    folders: dict,
    label: str,
    *,
    strip_vr: bool = False,
) -> tuple[str | None, str | None, str | None]:
    """Match a label against a folder map. Returns (folder_id, match_mode, matched_label)."""
    label = str(label or "").strip()
    if not label:
        return None, None, None
    candidates: list[str] = [label]
    if strip_vr:
        vr_label = _strip_vr_from_label(label)
        if vr_label and vr_label != label:
            candidates.append(vr_label)

    for needle in candidates:
        needle_fold = needle.casefold()
        for folder_id, folder_label in folders.items():
            if not isinstance(folder_label, str):
                continue
            cand = folder_label.strip()
            if cand.casefold() == needle_fold:
                return folder_id, "exact", cand
        norm_needle = _folder_match_key(needle)
        if not norm_needle:
            continue
        for folder_id, folder_label in folders.items():
            if not isinstance(folder_label, str):
                continue
            if _folder_match_key(folder_label) == norm_needle:
                return folder_id, "normalized", folder_label.strip()
    return None, None, None


def _gofile_folder_url_for_label(label: str) -> tuple[str | None, str | None]:
    """Resolve a GoFile gallery URL from `gofile-folders.json` by folder display name.

    Returns (url, match_mode) where match_mode is ``exact``, ``normalized``, or None.
    """
    if not label or not isinstance(label, str):
        return None, None
    needle = label.strip()
    if not needle:
        return None, None
    folders = _load_gofile_folders()
    needle_fold = needle.casefold()
    for folder_id, folder_label in folders.items():
        if not isinstance(folder_label, str):
            continue
        cand = folder_label.strip()
        if cand.casefold() == needle_fold:
            return folder_url(folder_id), "exact"
    norm_needle = _folder_match_key(needle)
    if not norm_needle:
        return None, None
    for folder_id, folder_label in folders.items():
        if not isinstance(folder_label, str):
            continue
        if _folder_match_key(folder_label) == norm_needle:
            return folder_url(folder_id), "normalized"
    return None, None


def _gofile_folder_url_search(
    label: str, *, strip_vr: bool = False
) -> tuple[str | None, str | None, str | None]:
    """Match a label against GoFile ``gofile-folders.json``."""
    folder_id, match_mode, matched_label = _folder_id_search(
        _load_gofile_folders(), label, strip_vr=strip_vr
    )
    if not folder_id:
        return None, None, None
    return folder_url(folder_id, provider="gofile"), match_mode, matched_label


def _filester_folder_url_search(
    label: str, *, strip_vr: bool = False
) -> tuple[str | None, str | None, str | None]:
    """Match a label against Filester folder map."""
    folder_id, match_mode, matched_label = _folder_id_search(
        _load_filester_folders(), label, strip_vr=strip_vr
    )
    if not folder_id:
        return None, None, None
    return folder_url(folder_id, provider="filester"), match_mode, matched_label


def _resolve_filester_folder_id(folder_id: str | None) -> str | None:
    """Map a UI folder selection to a Filester folder identifier."""
    folder_id = (folder_id or "").strip()
    if not folder_id:
        return None
    filester_folders = _load_filester_folders()
    if folder_id in filester_folders:
        return folder_id
    label = (_load_gofile_folders().get(folder_id) or "").strip()
    if not label:
        return None
    resolved, _mode, _matched = _folder_id_search(
        filester_folders, label, strip_vr=True
    )
    return resolved


def _gofile_folder_url_for_display_name(display_name: str) -> str | None:
    """Backward-compatible wrapper: URL only."""
    url, _mode = _gofile_folder_url_for_label(display_name)
    return url


def _studio_aliases_load():
    try:
        with open(STUDIO_ALIASES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _studio_aliases_save(data):
    os.makedirs(HASHES_DIR, exist_ok=True)
    tmp = STUDIO_ALIASES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, STUDIO_ALIASES_FILE)


def _studio_alias_key(stashdb_studio: str) -> str:
    return re.sub(r"\s+", " ", str(stashdb_studio or "").strip()).lower()


def _studio_threads_load():
    try:
        with open(STUDIO_THREADS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _studio_threads_save(data):
    os.makedirs(HASHES_DIR, exist_ok=True)
    tmp = STUDIO_THREADS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, STUDIO_THREADS_FILE)


def _studio_thread_key(stashdb_studio: str) -> str:
    return _studio_alias_key(stashdb_studio)


def _studio_thread_lookup(stashdb_studio: str) -> str | None:
    """Optional forum thread URL for a StashDB studio name."""
    needle = _studio_thread_key(stashdb_studio)
    if not needle:
        return None
    store = _studio_threads_load()
    row = store.get(needle)
    if isinstance(row, dict):
        url = (row.get("thread_url") or "").strip()
        if url:
            return url
    for key, row in store.items():
        if not isinstance(row, dict):
            continue
        sd = (row.get("stashdb_studio") or key or "").strip().casefold()
        if sd == needle:
            url = (row.get("thread_url") or "").strip()
            if url:
                return url
    return None


def _resolution_preset_key(width: int, height: int) -> str:
    return f"{int(width)}x{int(height)}"


def _resolution_presets_load() -> dict:
    try:
        with open(RESOLUTION_PRESETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _resolution_presets_save(data: dict) -> None:
    os.makedirs(HASHES_DIR, exist_ok=True)
    tmp = RESOLUTION_PRESETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, RESOLUTION_PRESETS_FILE)


def _normalize_resolution_label(label: str) -> str:
    """Marketing label only, e.g. ``7K`` from ``7K?`` or ``7K (6912x3456)``."""
    s = re.sub(r"\s*\(\d+x\d+\)\s*$", "", str(label or "").strip(), flags=re.I)
    s = re.sub(r"\?$", "", s).strip()
    m = re.match(r"^(\d+)K$", s, re.I)
    if m:
        return f"{int(m.group(1))}K"
    return ""


def _resolution_presets_learn(width: int, height: int, label: str) -> dict:
    w, h = int(width), int(height)
    if w < 1 or h < 1:
        raise ValueError("width and height must be positive")
    norm = _normalize_resolution_label(label)
    if not norm:
        raise ValueError("label must be a K rating like 7K")
    key = _resolution_preset_key(w, h)
    store = _resolution_presets_load()
    row = {
        "label": norm,
        "width": w,
        "height": h,
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    store[key] = row
    _resolution_presets_save(store)
    return row


def _studio_alias_lookup(stashdb_studio: str) -> dict | None:
    """Find alias row for a StashDB studio name (not exposed in GoFile folder picker)."""
    name = str(stashdb_studio or "").strip()
    if not name:
        return None
    data = _studio_aliases_load()
    row = data.get(_studio_alias_key(name))
    if isinstance(row, dict):
        return row
    needle = name.casefold()
    for row in data.values():
        if not isinstance(row, dict):
            continue
        sd = (row.get("stashdb_studio") or "").strip().casefold()
        if sd == needle:
            return row
    return None


def _resolve_studio_for_autofill(
    stashdb_studio: str, network_studio: str | None = None
) -> tuple[str, str | None, dict]:
    """Map StashDB substudio (+ optional parent network) → studio label + gallery URL.

    The returned studio label is always the substudio name (never rewritten to
    ``{substudio} / {network}``). When the substudio folder is missing but a parent
    network folder exists, ``meta["studio_link_label"]`` names the parent for BBCode
    linking. Manual ``studio_aliases.json`` overrides network fallback when the
    substudio folder is not found automatically.
    """
    substudio = str(stashdb_studio or "").strip()
    network_raw = str(network_studio or "").strip()
    if not network_raw and " / " in substudio:
        parsed_sub, parsed_net = _parse_studio_display_label(substudio)
        if parsed_net:
            substudio = parsed_sub
            network_raw = parsed_net
    network = _strip_network_suffix(network_raw) if network_raw else ""
    meta: dict = {"alias_applied": False}
    if not substudio:
        return "", None, meta

    has_distinct_network = bool(
        network and network.casefold() != substudio.casefold()
    )
    if has_distinct_network:
        meta["network_studio"] = network
        if network_raw and network_raw != network:
            meta["network_studio_raw"] = network_raw

    def _attach_thread() -> None:
        thread_url = _studio_thread_lookup(substudio)
        if not thread_url and network:
            thread_url = _studio_thread_lookup(network)
        if thread_url:
            meta["thread_url"] = thread_url

    def _attach_filester_network_fallback() -> None:
        if not FILESTER_ENABLED or meta.get("filester_gallery_link"):
            return
        fs_url, fs_mode, fs_label = _filester_folder_url_search(network, strip_vr=False)
        if fs_url:
            meta["filester_gallery_link"] = fs_url
            meta["filester_folder_match"] = fs_mode
            if fs_label:
                meta["filester_folder_label"] = fs_label

    gallery: str | None = None

    gf_url, gf_mode, gf_label = (
        _gofile_folder_url_search(substudio, strip_vr=True)
        if GOFILE_ENABLED
        else (None, None, None)
    )
    fs_url, fs_mode, fs_label = (
        _filester_folder_url_search(substudio, strip_vr=True)
        if FILESTER_ENABLED
        else (None, None, None)
    )
    if gf_url or fs_url:
        if gf_url:
            gallery = gf_url
            meta["folder_match"] = gf_mode
            meta["folder_label"] = gf_label
            meta["matched_level"] = "studio"
        if fs_url:
            meta["filester_gallery_link"] = fs_url
            meta["filester_folder_match"] = fs_mode
            if fs_label:
                meta["filester_folder_label"] = fs_label
            if not meta.get("matched_level"):
                meta["matched_level"] = "studio"
        _attach_thread()
        return substudio, gallery, meta

    alias = _studio_alias_lookup(substudio)
    if alias:
        meta["alias_applied"] = True
        meta["stashdb_studio"] = (alias.get("stashdb_studio") or substudio).strip()
        folder_label = (alias.get("folder_name") or "").strip()
        if folder_label:
            if GOFILE_ENABLED:
                gallery, match_mode, matched_label = _gofile_folder_url_search(
                    folder_label, strip_vr=False
                )
                if gallery:
                    meta["folder_match"] = match_mode
                    meta["folder_label"] = matched_label or folder_label
                    meta["matched_level"] = "alias"
            if FILESTER_ENABLED and not meta.get("filester_gallery_link"):
                fs_alias_url, fs_alias_mode, fs_alias_label = _filester_folder_url_search(
                    folder_label, strip_vr=False
                )
                if fs_alias_url:
                    meta["filester_gallery_link"] = fs_alias_url
                    meta["filester_folder_match"] = fs_alias_mode
                    if fs_alias_label:
                        meta["filester_folder_label"] = fs_alias_label
                    if not meta.get("matched_level"):
                        meta["matched_level"] = "alias"
        studio_out = (alias.get("studio_display") or "").strip() or substudio
        _attach_thread()
        return studio_out, gallery, meta

    if network:
        if GOFILE_ENABLED:
            gf_net_url, gf_net_mode, gf_net_label = _gofile_folder_url_search(
                network, strip_vr=False
            )
            if gf_net_url:
                gallery = gf_net_url
                meta["folder_match"] = gf_net_mode
                meta["folder_label"] = gf_net_label
        _attach_filester_network_fallback()
        if gallery or meta.get("filester_gallery_link"):
            meta["matched_level"] = "network"
            meta["studio_link_label"] = network
            _attach_thread()
            return substudio, gallery, meta

    _attach_thread()
    _attach_filester_network_fallback()
    return substudio, gallery, meta


@app.route("/api/resolve_studio", methods=["POST"])
def api_resolve_studio():
    """Resolve BBCode studio label + GoFile gallery from StashDB studio/network names."""
    data = request.get_json(silent=True) or {}
    studio = (data.get("studio") or "").strip()
    if not studio:
        return jsonify({"error": "studio is required"}), 400
    network = (data.get("network") or data.get("network_studio") or "").strip() or None
    studio_out, gallery_url, meta = _resolve_studio_for_autofill(studio, network)
    payload = {
        "ok": True,
        "studio": studio_out,
        "stashdb_studio": studio,
        "network_studio": network or "",
        "meta": meta,
    }
    if gallery_url and GOFILE_ENABLED:
        payload["gallery_link"] = gallery_url
        payload["studio_folder_url"] = gallery_url
        payload["gallery_match"] = meta.get("folder_match")
    fs_gallery = meta.get("filester_gallery_link")
    if fs_gallery:
        payload["filester_gallery_link"] = fs_gallery
    link_label = (meta.get("studio_link_label") or "").strip()
    if link_label:
        payload["studio_link_label"] = link_label
    thread_url = meta.get("thread_url")
    if thread_url:
        payload["thread_url"] = thread_url
    return jsonify(payload)


def _bbcode_helper_url(
    scene_id: str,
    filename: str,
    entry: dict | None = None,
    gallery_link: str | None = None,
    *,
    gallery_filester: str | None = None,
    split_parts: int | None = None,
    split_name: str | None = None,
    split_mode: str | None = None,
) -> str:
    """Build `/bbcode` query string with optional video dimensions from cache."""
    q = [
        ("scene_id", scene_id),
        ("filename", filename),
    ]
    if entry:
        vw = entry.get("video_width")
        vh = entry.get("video_height")
        try:
            if vw is not None and vh is not None:
                q.append(("vw", str(int(vw))))
                q.append(("vh", str(int(vh))))
        except (TypeError, ValueError):
            pass
    if gallery_link:
        q.append(("gallery", gallery_link))
    if gallery_filester:
        q.append(("gallery_filester", gallery_filester))
    if split_parts and int(split_parts) > 1:
        q.append(("split_parts", str(int(split_parts))))
        if split_name:
            q.append(("split_name", split_name))
        if split_mode:
            q.append(("split_mode", split_mode))
    return "/bbcode?" + urllib.parse.urlencode(q)


def _performers_load():
    try:
        with open(PERFORMERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _performers_save(data):
    os.makedirs(HASHES_DIR, exist_ok=True)
    tmp = PERFORMERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, PERFORMERS_FILE)


def _performer_key(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip()).lower()


def _performers_lookup(names):
    data = _performers_load()
    out = {}
    for raw in names or []:
        name = str(raw or "").strip()
        if not name:
            continue
        row = data.get(_performer_key(name))
        if isinstance(row, dict) and row.get("link"):
            out[name] = row.get("link")
    return out


def _performers_upsert(rows):
    data = _performers_load()
    changed = 0
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        link = str(row.get("link") or "").strip()
        if not name or not link:
            continue
        k = _performer_key(name)
        if not k:
            continue
        prev = data.get(k) or {}
        if prev.get("link") != link or prev.get("name") != name:
            changed += 1
        data[k] = {"name": name, "link": link, "updated_at": now}
    if changed:
        _performers_save(data)
    return changed


def _ensure_root():
    global _gofile_root_id
    if _gofile_root_id is None:
        _gofile_root_id = get_root_folder_id()
        print(f"[GOFILE] Root folder: {_gofile_root_id}", flush=True)
    return _gofile_root_id


def _gofile_root_id_optional() -> str | None:
    """Best-effort root folder id; None when GoFile API is unavailable."""
    try:
        return _ensure_root()
    except Exception as e:
        print(f"[GOFILE] Root folder lookup failed: {e}", flush=True)
        return None


# ── Scene cache (file-backed; survives container restarts and file deletions) ──
# Stored in cache/scenes.json (renamed from hashes.json).
#
# Layout, keyed by path under the downloads mount (``clip.mp4`` or ``vrp/clip.mp4``):
#   { "<filename>": {
#       "size_bytes": int,        # for staleness check
#       "duration_int": int|null, # ffprobe duration, kept once any algo computes it
#       "computed_at": "ISO-8601",
#       "oshash": "...", "phash": "...",   # any subset present
#       "video_width": int|null, "video_height": int|null,  # ffprobe v:0 (hasher-http)
#       "stashdb": {                                     # optional: most recent match / link
#           "scene_id": "...", "edit_id": "...", "draft_id": "...",  # any subset
#           "matched_by": "OSHASH"|"PHASH"|"AUTOFILL"|"EDIT"|"DRAFT"|"MANUAL",
#           "checked_at": "ISO-8601",
#           "contributed": ["OSHASH:<hash>", ...]        # submitFingerprint successes (idempotency)
#       }
#       "upload": {                                      # set after successful upload
#           "gofile_url", "filester_url", "was_split",
#           "filester_folder_url" (split), "filester_file_url" (single file),
#           "recorded_at"
#       }
#       "split": {                                       # split metadata from upload
#           "was_split", "part_count", "original_basename", "split_mode",
#           "folder_url" (Filester split folder), "recorded_at"
#       }
#       "bbcode": {                                      # last manual BBCode row saved on /bbcode copy
#           "studio", "gallery_link", "title", "upload_date", "resolution",
#           "stashdb_url", "saved_at"
#       }
#   } }

_scenes_lock = threading.Lock()
_scenes_migrated = False


def _scenes_ensure_migrated():
    global _scenes_migrated
    if _scenes_migrated:
        return
    _scenes_migrated = True
    if os.path.isfile(SCENES_FILE):
        return
    if os.path.isfile(_LEGACY_HASHES_FILE):
        try:
            os.replace(_LEGACY_HASHES_FILE, SCENES_FILE)
            print(f"[SCENES] Migrated {_LEGACY_HASHES_FILE} -> {SCENES_FILE}", flush=True)
        except OSError as e:
            print(f"[SCENES] Migration failed: {e}", flush=True)


def _scenes_load():
    """Read scenes.json (returns {} on first run / corrupt file)."""
    _scenes_ensure_migrated()
    try:
        with open(SCENES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _scenes_save(data):
    """Atomically write scenes.json (temp + os.replace)."""
    os.makedirs(HASHES_DIR, exist_ok=True)
    _scenes_ensure_migrated()
    tmp = SCENES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, SCENES_FILE)


def _scenes_get(filename, expected_size=None):
    """Return cached entry for `filename` (basename), or None.

    If `expected_size` is provided and differs from the cached entry's
    size_bytes, the entry is considered stale and None is returned.
    """
    if not filename:
        return None
    with _scenes_lock:
        data = _scenes_load()
        entry = data.get(filename)
        if not isinstance(entry, dict):
            return None
        if expected_size is not None:
            cached_size = entry.get("size_bytes")
            if cached_size is not None and cached_size != expected_size:
                return None
        return dict(entry)


def _scenes_set(filename, **fields):
    """Merge fields into the cached entry for filename and persist.

    Always stamps `computed_at` to the current UTC time.
    Returns the updated entry.
    """
    if not filename:
        return None
    with _scenes_lock:
        data = _scenes_load()
        entry = data.get(filename)
        if not isinstance(entry, dict):
            entry = {}
        entry.update(fields)
        entry["computed_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        data[filename] = entry
        _scenes_save(data)
        return dict(entry)


def _scenes_merge_stashdb_link(
    filename: str,
    *,
    scene_id: str | None = None,
    edit_id: str | None = None,
    draft_id: str | None = None,
    matched_by: str | None = None,
) -> dict:
    """Merge StashDB scene/edit/draft ids into scenes.json for a basename (creates entry if needed)."""
    if not filename:
        return {}
    scene_id = (scene_id or "").strip() or None
    edit_id = (edit_id or "").strip() or None
    draft_id = (draft_id or "").strip() or None
    if not scene_id and not edit_id and not draft_id:
        return {}

    checked_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _scenes_lock:
        data = _scenes_load()
        entry = data.get(filename)
        if not isinstance(entry, dict):
            entry = {}
        prev = entry.get("stashdb") if isinstance(entry.get("stashdb"), dict) else {}
        stash = dict(prev)
        if scene_id:
            if stash.get("scene_id") and stash.get("scene_id") != scene_id:
                stash["contributed"] = []
            stash["scene_id"] = scene_id
        if edit_id:
            stash["edit_id"] = edit_id
        if draft_id:
            stash["draft_id"] = draft_id
        if matched_by:
            stash["matched_by"] = matched_by
        elif scene_id and not stash.get("matched_by"):
            stash["matched_by"] = "AUTOFILL"
        elif edit_id and not scene_id and not stash.get("matched_by"):
            stash["matched_by"] = "EDIT"
        elif draft_id and not scene_id and not stash.get("matched_by"):
            stash["matched_by"] = "DRAFT"
        stash["checked_at"] = checked_at
        entry["stashdb"] = stash
        data[filename] = entry
        _scenes_save(data)
        return dict(stash)


def _scenes_clear_match(filename):
    """Remove only the stashdb sub-object for filename (keep hash fields)."""
    if not filename:
        return
    with _scenes_lock:
        data = _scenes_load()
        entry = data.get(filename)
        if isinstance(entry, dict) and "stashdb" in entry:
            entry.pop("stashdb", None)
            data[filename] = entry
            _scenes_save(data)


def _scenes_merge_bbcode(
    filename: str,
    *,
    studio: str = "",
    gallery_link: str = "",
    title: str = "",
    upload_date: str = "",
    resolution: str = "",
    stashdb_url: str = "",
    scene_id: str | None = None,
) -> dict:
    """Persist manual BBCode row edits for a basename (e.g. on /bbcode copy)."""
    if not filename:
        return {}
    saved_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    bbcode = {
        "studio": (studio or "").strip(),
        "gallery_link": (gallery_link or "").strip(),
        "title": (title or "").strip(),
        "upload_date": (upload_date or "").strip(),
        "resolution": (resolution or "").strip(),
        "stashdb_url": (stashdb_url or "").strip(),
        "saved_at": saved_at,
    }
    with _scenes_lock:
        data = _scenes_load()
        entry = data.get(filename)
        if not isinstance(entry, dict):
            entry = {}
        entry["bbcode"] = bbcode
        sid = (scene_id or "").strip()
        if sid:
            prev = entry.get("stashdb") if isinstance(entry.get("stashdb"), dict) else {}
            stash = dict(prev)
            if stash.get("scene_id") != sid:
                stash["contributed"] = []
            stash["scene_id"] = sid
            if not stash.get("matched_by"):
                stash["matched_by"] = "MANUAL"
            stash["checked_at"] = saved_at
            entry["stashdb"] = stash
        data[filename] = entry
        _scenes_save(data)
    return bbcode


def _remote_url_cache_filename(url: str) -> str:
    """Stable basename under /downloads for scenes.json (remote URL jobs)."""
    raw = (url or "").strip().encode("utf-8", errors="ignore")
    digest = hashlib.sha256(raw).hexdigest()[:48]
    try:
        path = urllib.parse.urlparse((url or "").strip()).path
        ext = os.path.splitext(path)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            ext = ".mp4"
    except Exception:  # noqa: BLE001
        ext = ".mp4"
    return f"remote_{digest}{ext}"


def _stashdb_lookup_scene_ordered(fps: list[tuple[str, str]]):
    """Try (algorithm, hash) pairs in order; return (raw_scene, matched_algo) or (None, None)."""
    for algo, h in fps:
        algo = (algo or "").strip().upper()
        h = (h or "").strip()
        if not algo or not h:
            continue
        try:
            matches = _stashdb_find_by_fingerprints([{"algorithm": algo, "hash": h}])
        except Exception:  # noqa: BLE001
            continue
        if matches:
            return matches[0], algo
    return None, None


def _remote_download_delete_enabled() -> bool:
    return _env_yes("REMOTE_HASH_DELETE_FILE", default="1")


def _maybe_delete_remote_download(path: str | None) -> None:
    if not path or not _remote_download_delete_enabled():
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
            print(f"[REMOTE_HASH] removed temp download {path}", flush=True)
    except OSError as e:
        print(f"[REMOTE_HASH] could not remove {path}: {e}", flush=True)


def _folder_display_name(folder_id):
    if not folder_id:
        return "Root"
    gofile_folders = _load_gofile_folders()
    if folder_id in gofile_folders:
        return gofile_folders[folder_id]
    filester_folders = _load_filester_folders()
    if folder_id in filester_folders:
        return filester_folders[folder_id]
    return folder_id[:12]


def _is_video_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(path)
    return bool(mime and mime.startswith("video/"))


def _thumber_docker_executable():
    if THUMBER_DOCKER_BIN:
        return THUMBER_DOCKER_BIN
    return shutil.which("docker")


def _thumber_http_extra_headers():
    raw = (os.environ.get("THUMBER_HTTP_HEADERS_JSON") or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        print("[CONFIG] Invalid THUMBER_HTTP_HEADERS_JSON", flush=True)
        return {}


def _thumber_http_bearer_token():
    """Match thumber's THUMBER_HTTP_TOKEN; THUMBER_HTTP_BEARER kept as alias."""
    return (THUMBER_HTTP_TOKEN or THUMBER_HTTP_BEARER or "").strip()


def _thumber_http_headers():
    h = {"Content-Type": "application/json", **_thumber_http_extra_headers()}
    token = _thumber_http_bearer_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _thumber_http_headers_for_post(stream=False):
    h = _thumber_http_headers()
    if stream:
        # Some SSE stacks expect these; harmless if ignored.
        h["Accept"] = "text/event-stream"
        h["Cache-Control"] = "no-cache"
    return h


def _thumber_http_stream_url():
    """
    Resolve POST /v1/thumbs/stream URL from THUMBER_HTTP_URL (base or /v1/thumbs).
    apu uses SSE streaming only (no sync /v1/thumbs client).
    """
    u = THUMBER_HTTP_URL.strip().rstrip("/")
    if not u:
        return None
    if u.endswith("/v1/thumbs/stream"):
        return u
    if u.endswith("/v1/thumbs"):
        return f"{u}/stream"
    return f"{u}/v1/thumbs/stream"


def _apply_thumber_v1_json(job_id, data):
    """Map thumber-http JSON error bodies into logs. Returns (ok, exit_code)."""
    if not isinstance(data, dict):
        _append_thumber_log(job_id, str(data))
        return None, None
    out_path = data.get("output_path")
    if out_path:
        _append_thumber_log(job_id, f"output_path: {out_path}")
    fn = data.get("filename")
    if fn and fn != out_path:
        _append_thumber_log(job_id, f"filename: {fn}")
    ec = data.get("exit_code")
    if ec is not None:
        _append_thumber_log(job_id, f"exit_code: {ec}")
    logs = data.get("logs")
    if isinstance(logs, str):
        for line in logs.splitlines():
            _append_thumber_log(job_id, line)
    elif isinstance(logs, list):
        for item in logs:
            _append_thumber_log(job_id, str(item))
    err = data.get("error")
    if err:
        _append_thumber_log(job_id, str(err))
    return data.get("ok"), ec


def _thumber_http_post(job_id, url, payload, stream, max_attempts=10):
    """POST with retries on 503 busy (thumber-http runs one job at a time)."""
    timeout = (30, THUMBER_HTTP_TIMEOUT)
    delay = 2.0
    last = None
    headers = _thumber_http_headers_for_post(stream=stream)
    for attempt in range(max_attempts):
        try:
            r = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
                stream=stream,
            )
        except requests.RequestException as e:
            last = e
            _append_thumber_log(job_id, f"HTTP request failed (attempt {attempt + 1}): {e}")
            print(f"[THUMBER] HTTP error: {e}", flush=True)
            if attempt >= max_attempts - 1:
                raise
            time.sleep(min(delay, 30))
            delay = min(delay * 1.5, 30)
            continue
        if r.status_code == 503:
            try:
                hint = r.json()
            except ValueError:
                hint = r.text[:300] if r.text else ""
            r.close()
            _append_thumber_log(
                job_id,
                f"thumber-http busy (503), retry in {int(delay)}s — {hint}",
            )
            print("[THUMBER] thumber-http 503 busy, retrying", flush=True)
            time.sleep(delay)
            delay = min(delay * 1.5, 30)
            continue
        return r
    if last:
        raise last
    raise RuntimeError("thumber HTTP: retries exhausted")


def _run_thumber_http_stream(job_id, filename, url, *, parallel_with_upload: bool = False):
    payload = {"filename": filename}
    _append_thumber_log(job_id, f"POST {url}  (stream)  {json.dumps(payload)}")
    print(f"[THUMBER] HTTP stream {url}  filename={filename!r}", flush=True)
    try:
        r = _thumber_http_post(job_id, url, payload, stream=True, max_attempts=10)
    except requests.RequestException as e:
        _append_thumber_log(job_id, f"HTTP gave up: {e}")
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Thumber HTTP unreachable; continuing with upload..."
        return
    if not r.ok:
        try:
            data = r.json()
            _apply_thumber_v1_json(job_id, data)
        except ValueError:
            for line in (r.text or "").splitlines():
                _append_thumber_log(job_id, line)
        r.close()
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Thumbnail generation failed; continuing with upload..."
        print(f"[THUMBER] HTTP stream failed {r.status_code}", flush=True)
        return

    done_ok = False
    try:
        with r:
            for raw in r.iter_lines(decode_unicode=True, chunk_size=1):
                if _is_cancelled(job_id):
                    _append_thumber_log(job_id, "(cancelled — closing thumber stream)")
                    break
                if raw is None:
                    continue
                line = raw.strip()
                if not line.startswith("data: "):
                    if line:
                        _append_thumber_log(job_id, line)
                    continue
                try:
                    obj = json.loads(line[6:])
                except ValueError:
                    _append_thumber_log(job_id, line)
                    continue
                if not isinstance(obj, dict):
                    continue
                evt = obj.get("type")
                if evt == "log":
                    ln = obj.get("line") or ""
                    if ln:
                        _append_thumber_log(job_id, ln)
                        if not parallel_with_upload:
                            jobs[job_id]["status_text"] = f"Generating thumbnails... {ln[:120]}"
                elif evt == "error":
                    detail = obj.get("detail") or obj.get("error") or str(obj)
                    _append_thumber_log(job_id, f"[thumber] {detail}")
                elif evt == "done":
                    if obj.get("output_path"):
                        _append_thumber_log(job_id, f"output_path: {obj['output_path']}")
                    if obj.get("exit_code") is not None:
                        _append_thumber_log(job_id, f"exit_code: {obj['exit_code']}")
                    done_ok = bool(obj.get("ok")) and int(obj.get("exit_code", 0)) == 0
                    if not done_ok and obj.get("error"):
                        _append_thumber_log(job_id, str(obj["error"]))
                    break
    except requests.exceptions.ChunkedEncodingError as e:
        _append_thumber_log(job_id, f"[thumber] stream ended early: {e}")
    except OSError as e:
        _append_thumber_log(job_id, f"[thumber] stream read error: {e}")

    if _is_cancelled(job_id):
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Thumbnail step cancelled; preparing upload..."
        return
    if done_ok:
        _append_thumber_log(job_id, "thumber HTTP stream completed successfully")
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Thumbnails generated. Preparing upload..."
        print("[THUMBER] HTTP stream done ok", flush=True)
    else:
        _append_thumber_log(job_id, "thumber stream ended without success")
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Thumbnail generation failed; continuing with upload..."
        print("[THUMBER] HTTP stream incomplete or failed", flush=True)


def _run_thumber_via_http(job_id, filename, _full_path, *, parallel_with_upload: bool = False):
    """thumber-http API: POST /v1/thumbs/stream (SSE) only; see THUMBER_HTTP.md."""
    url = _thumber_http_stream_url()
    if not url:
        _append_thumber_log(job_id, "THUMBER_HTTP_URL is not set")
        return
    _run_thumber_http_stream(job_id, filename, url, parallel_with_upload=parallel_with_upload)


def _append_thumber_log(job_id, line):
    line = line.rstrip()
    if not line:
        return
    job = jobs.get(job_id)
    if not job:
        return
    log_lines = job.setdefault("thumber_logs", [])
    log_lines.append(line)
    if len(log_lines) > THUMBER_LOG_LIMIT:
        del log_lines[:-THUMBER_LOG_LIMIT]


def _append_job_log(job_id, line):
    line = line.rstrip()
    if not line:
        return
    job = jobs.get(job_id)
    if not job:
        return
    log_lines = job.setdefault("job_logs", [])
    log_lines.append(line)
    if len(log_lines) > THUMBER_LOG_LIMIT:
        del log_lines[:-THUMBER_LOG_LIMIT]


def _append_hasher_log(job_id, line):
    _append_job_log(job_id, f"[hasher] {line}")


def _make_hasher_stream_progress_callback(job_id: str, algorithm: str):
    """NDJSON stream hook for hasher-http: publish PHASH heartbeat state on the job dict."""
    algo_u = (algorithm or "").strip().upper()
    if algo_u not in HASHER_ALGORITHMS:
        return None
    read_timeout = HASHER_HTTP_TIMEOUT if algo_u == "PHASH" else 300
    time_budget = float(read_timeout)

    def on_event(ev: dict):
        if _is_cancelled(job_id):
            return
        job = jobs.get(job_id)
        if not job:
            return
        if str(ev.get("algorithm") or "").upper() != algo_u:
            return
        et = ev.get("type")
        if et != "heartbeat":
            return
        elapsed = ev.get("elapsed_sec")
        tail_raw = str(ev.get("stderr_tail") or "")
        tail = tail_raw.replace("\r\n", " ").replace("\n", " ").strip()
        if len(tail) > 200:
            tail = "…" + tail[-199:]
        ff_time = None
        m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", tail_raw)
        if m:
            ff_time = f"{m.group(1)}:{m.group(2)}:{m.group(3)}"
        rec: dict = {
            "algorithm": algo_u,
            "phase": ev.get("phase") or "running",
            "elapsed_sec": elapsed,
            "budget_sec": int(time_budget),
            "stderr_tail": tail,
        }
        if ff_time:
            rec["ffmpeg_time"] = ff_time
        if time_budget > 0 and isinstance(elapsed, (int, float)):
            rec["budget_pct"] = min(99.9, round((float(elapsed) / time_budget) * 100.0, 1))
        if ev.get("pct") is not None:
            try:
                rec["sprite_pct"] = round(float(ev["pct"]), 2)
            except (TypeError, ValueError):
                pass
        if ev.get("phash_frame") is not None:
            try:
                rec["phash_frame"] = int(ev["phash_frame"])
            except (TypeError, ValueError):
                pass
        if ev.get("phash_frames_total") is not None:
            try:
                rec["phash_frames_total"] = int(ev["phash_frames_total"])
            except (TypeError, ValueError):
                pass
        if ev.get("eta_sec") is not None:
            try:
                rec["eta_sec"] = round(float(ev["eta_sec"]), 1)
            except (TypeError, ValueError):
                pass
        job["hash_progress"] = rec

    return on_event


def _run_thumber(
    job_id,
    downloaded_path,
    reset_logs=True,
    *,
    parallel_with_upload: bool = False,
):
    if not _is_video_file(downloaded_path):
        return

    if not THUMBER_ENABLED:
        return

    dl_key = _downloads_key_for_sidecars(downloaded_path)
    display_name = os.path.basename(downloaded_path)

    if THUMBER_USE_CACHED_THUMBS and _thumb_sheet_path(dl_key):
        if not parallel_with_upload:
            jobs[job_id]["status"] = "processing"
        _append_thumber_log(
            job_id,
            f"using cached thumbnail sheet ({_thumb_sheet_basename(dl_key)}) — skipping thumber run",
        )
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Using cached thumbnails. Preparing upload..."
        return

    if not parallel_with_upload:
        jobs[job_id]["status"] = "processing"
        jobs[job_id]["status_text"] = f"Generating thumbnails for {display_name}..."
        jobs[job_id]["progress"] = {"type": "thumber"}
    if reset_logs:
        jobs[job_id]["thumber_logs"] = []

    if THUMBER_HTTP_URL:
        _run_thumber_via_http(job_id, dl_key, downloaded_path, parallel_with_upload=parallel_with_upload)
        return

    docker_bin = _thumber_docker_executable()
    if not docker_bin:
        _append_thumber_log(
            job_id,
            "docker not found (no Docker CLI in this environment). "
            "Thumbnails skipped — upload will continue. "
            "Set THUMBER_HTTP_URL to call thumber over HTTP, or mount Docker socket + CLI, "
            "or set THUMBER_ENABLED=0 to hide this.",
        )
        print(
            "[THUMBER] docker executable not found; skipping thumbnails "
            "(use THUMBER_HTTP_URL from inside Docker — see README)",
            flush=True,
        )
        return

    command = [
        docker_bin, "compose",
        "-f", THUMBER_COMPOSE_FILE,
        "--project-directory", THUMBER_PROJECT_DIR,
        "run", "--rm", THUMBER_SERVICE, dl_key
    ]

    _append_thumber_log(job_id, f"$ {' '.join(command)}")

    print(f"[THUMBER] Running: {' '.join(command)}", flush=True)
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
    except FileNotFoundError as e:
        _append_thumber_log(
            job_id,
            f"Could not run {docker_bin!r}: {e}. Skipping thumbnails; continuing upload.",
        )
        print(f"[THUMBER] {e}", flush=True)
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Thumbnail step skipped; preparing upload..."
        return
    rc = -1
    try:
        for line in proc.stdout:
            if _is_cancelled(job_id):
                proc.terminate()
                proc.wait()
                return
            _append_thumber_log(job_id, line)
            short = line.strip()
            if short and not parallel_with_upload:
                jobs[job_id]["status_text"] = f"Generating thumbnails... {short[:120]}"
        rc = proc.wait()
    finally:
        if proc.stdout:
            proc.stdout.close()

    if rc != 0:
        msg = f"thumber failed with exit code {rc}"
        _append_thumber_log(job_id, msg)
        print(f"[THUMBER] {msg}", flush=True)
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Thumbnail generation failed; continuing with upload..."
    else:
        _append_thumber_log(job_id, "thumber completed successfully")
        if not parallel_with_upload:
            jobs[job_id]["status_text"] = "Thumbnails generated. Preparing upload..."


# ── Pages ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/bbcode")
def bbcode_page():
    scene_id = (request.args.get("scene_id") or "").strip()
    filename = (request.args.get("filename") or "").strip()
    gallery = (request.args.get("gallery") or "").strip()
    gallery_filester = (request.args.get("gallery_filester") or "").strip()
    split_parts = 0
    try:
        split_parts = int((request.args.get("split_parts") or "0").strip())
    except (TypeError, ValueError):
        split_parts = 0
    split_name = (request.args.get("split_name") or "").strip()
    split_mode = (request.args.get("split_mode") or "").strip()
    return render_template(
        "bbcode.html",
        scene_id=scene_id,
        filename=filename,
        gallery=gallery,
        gallery_filester=gallery_filester,
        split_parts=split_parts,
        split_name=split_name,
        split_mode=split_mode,
        active_providers=ACTIVE_PROVIDERS,
        gofile_enabled=GOFILE_ENABLED,
        filester_enabled=FILESTER_ENABLED,
        filester_merge_parts_guide_url=FILESTER_MERGE_PARTS_GUIDE_URL,
        filester_merge_parts_guide_size=FILESTER_MERGE_PARTS_GUIDE_SIZE,
        filester_split_mode=FILESTER_SPLIT_MODE,
        stashdb_scenes_base=STASHDB_SCENES_BASE,
        goonbox_base_url=GOONBOX_BASE_URL,
        gifyu_base_url=GIFYU_BASE_URL,
        gif_host=GIF_HOST,
        pixhost_enabled=(GIF_HOST == "pixhost"),
    )


@app.route("/bbcode-helper")
def bbcode_helper_redirect():
    """Legacy URL from upload results; same page as /bbcode."""
    qs = request.query_string.decode("utf-8", errors="replace")
    target = "/bbcode" + (f"?{qs}" if qs else "")
    return redirect(target, code=302)


@app.route("/stashdb-scene-draft")
def stashdb_scene_draft_page():
    return render_template("stashdb_scene_draft.html")


@app.route("/oshash")
def oshash_page():
    return render_template("oshash.html")


@app.route("/phash")
def phash_page():
    return render_template(
        "phash.html",
        stashdb_can_submit=bool(STASHDB_API_KEY and STASHDB_AUTO_CONTRIBUTE),
    )


@app.route("/remotehash")
def remotehash_page():
    return render_template("remotehash.html")


@app.route("/aliases")
def studio_aliases_page():
    """Studio → GoFile folder aliases (not listed in the upload folder picker)."""
    return render_template("aliases.html")


@app.route("/api/scenes_cache_update", methods=["POST"])
@app.route("/api/hashes_cache_update", methods=["POST"])
def api_scenes_cache_update():
    """Merge optional hash fields into scenes.json for a file under /downloads (basename or subpath)."""
    data = request.get_json(silent=True) or {}
    try:
        name = _safe_downloads_mount_rel((data.get("filename") or "").strip())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    fields = {}
    if data.get("oshash"):
        o = re.sub(r"[^a-fA-F0-9]", "", str(data["oshash"]).lower())
        if len(o) == 16:
            fields["oshash"] = o
    if data.get("phash"):
        p = re.sub(r"[^a-fA-F0-9]", "", str(data["phash"]).lower())
        if len(p) == 16:
            fields["phash"] = p
    if data.get("duration_int") is not None:
        try:
            di = int(data["duration_int"])
            if di >= 0:
                fields["duration_int"] = di
        except (TypeError, ValueError):
            pass
    if not fields:
        return jsonify({"error": "no valid fields to merge"}), 400
    _scenes_set(name, **fields)
    return jsonify({"ok": True, "filename": name, "updated": list(fields.keys())})


@app.route("/api/phash_from_url", methods=["POST"])
def api_phash_from_url():
    """Full download + hasher-http PHASH; OSHASH via Range when possible; StashDB; hashes.json."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400
    if not _hasher_service_active():
        return jsonify({"ok": False, "error": "Hasher is not configured"}), 503
    if not _hasher_algo_enabled("PHASH"):
        return jsonify({"ok": False, "error": "PHASH is disabled (HASHER_PHASH_ENABLED)"}), 403

    osh_remote = fetch_oshash_from_url(url)
    dl_path: str | None = None
    try:
        dl_path = download_file(url)
        cache_name = _remote_url_cache_filename(url)
        target = os.path.join(DOWNLOADS_DIR, cache_name)
        if dl_path != target:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
            shutil.move(dl_path, target)
            dl_path = target
        cache_name = os.path.basename(dl_path)

        ph_body = _hash_via_sidecar(cache_name, "PHASH")
        phash_hex = (ph_body.get("hash") or "").strip().lower()
        if not phash_hex or len(phash_hex) != 16 or not re.fullmatch(r"[0-9a-f]{16}", phash_hex):
            return jsonify({"ok": False, "error": "hasher returned no valid PHASH"}), 500

        osh_hash = ""
        if osh_remote.get("ok") and osh_remote.get("hash"):
            osh_hash = str(osh_remote["hash"]).strip().lower()
        if not osh_hash or len(osh_hash) != 16:
            ob = _hash_via_sidecar(cache_name, "OSHASH")
            osh_hash = (ob.get("hash") or "").strip().lower()

        size_b = os.path.getsize(os.path.join(DOWNLOADS_DIR, cache_name))
        dur_int = ph_body.get("duration_int")
        if dur_int is None and osh_remote.get("ok"):
            dur_int = osh_remote.get("duration_int")
        if dur_int is not None:
            try:
                dur_int = int(dur_int)
            except (TypeError, ValueError):
                dur_int = None

        cache_fields = {
            "size_bytes": size_b,
            "oshash": osh_hash,
            "phash": phash_hex,
            "duration_int": dur_int,
        }
        if ph_body.get("width") is not None:
            cache_fields["video_width"] = ph_body.get("width")
        if ph_body.get("height") is not None:
            cache_fields["video_height"] = ph_body.get("height")
        _scenes_set(cache_name, **{k: v for k, v in cache_fields.items() if v is not None})

        payload: dict = {
            "ok": True,
            "url": url,
            "hash": phash_hex,
            "oshash": osh_hash,
            "cache_filename": cache_name,
            "size_bytes": size_b,
            "final_url": osh_remote.get("final_url") if osh_remote.get("ok") else None,
        }
        if dur_int is not None:
            payload["duration_int"] = dur_int
            payload["duration"] = float(dur_int)
        if osh_remote.get("ok") and osh_remote.get("duration_detail"):
            payload["duration_detail"] = osh_remote.get("duration_detail")

        if STASHDB_API_KEY:
            try:
                raw_scene, hit_algo = _stashdb_lookup_scene_ordered(
                    [("OSHASH", osh_hash), ("PHASH", phash_hex)]
                )
                if raw_scene:
                    payload["stashdb_match"] = _scene_fragment_to_match(raw_scene)
                    payload["stashdb_hit_by"] = hit_algo
                    if payload.get("duration_int") is None:
                        dsec, dint, det = _stashdb_duration_fallback_from_fingerprint(
                            raw_scene, hit_algo, osh_hash if hit_algo == "OSHASH" else phash_hex
                        )
                        if dint is not None:
                            payload["duration"] = dsec
                            payload["duration_int"] = dint
                            payload["duration_detail"] = det
                else:
                    payload["stashdb_match"] = None
            except Exception as e:  # noqa: BLE001
                payload["stashdb_error"] = str(e)
        else:
            payload["stashdb_skipped"] = "STASHDB_API_KEY is not set"

        payload["stashdb_can_submit"] = bool(STASHDB_API_KEY and STASHDB_AUTO_CONTRIBUTE)
        return jsonify(payload)
    except TransferCancelled:
        return jsonify({"ok": False, "error": "download cancelled"}), 499
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _maybe_delete_remote_download(dl_path)


def _generate_url_fingerprint_job_ndjson(url: str):
    """NDJSON stream: download progress, hasher events (PHASH/OSHASH), StashDB, ``done``."""
    yield json.dumps({"type": "phase", "phase": "start", "url": url}, ensure_ascii=False) + "\n"
    dl_path: str | None = None
    try:
        yield json.dumps({"type": "phase", "phase": "remote_oshash"}) + "\n"
        osh_remote = fetch_oshash_from_url(url)

        dq: queue.Queue = queue.Queue()

        def on_dl(pct, downloaded, total, speed, eta):
            dq.put(
                {
                    "type": "download",
                    "pct": pct,
                    "downloaded": downloaded,
                    "total": total,
                    "speed": speed,
                    "eta": eta,
                    "downloaded_fmt": format_size(downloaded),
                    "total_fmt": format_size(total) if total else "?",
                    "speed_fmt": f"{format_size(speed)}/s" if speed > 0 else "",
                }
            )

        err = [None]
        dl_result: list[str | None] = [None]

        def run_dl() -> None:
            try:
                dl_result[0] = download_file(url, on_progress=on_dl)
            except BaseException as e:  # noqa: BLE001
                err[0] = e

        t_dl = threading.Thread(target=run_dl, daemon=True)
        t_dl.start()
        while t_dl.is_alive():
            try:
                ev = dq.get(timeout=0.2)
                yield json.dumps(ev, ensure_ascii=False) + "\n"
            except queue.Empty:
                pass
        t_dl.join()
        if err[0] is not None:
            raise err[0]
        dl_path = dl_result[0]
        if not dl_path:
            raise RuntimeError("download returned no path")

        cache_name = _remote_url_cache_filename(url)
        target = os.path.join(DOWNLOADS_DIR, cache_name)
        if dl_path != target:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
            shutil.move(dl_path, target)
            dl_path = target
        cache_name = os.path.basename(dl_path)

        yield json.dumps({"type": "phase", "phase": "hashing_phash", "cache_filename": cache_name}) + "\n"
        hq: queue.Queue = queue.Queue()
        ph_err = [None]
        ph_body: list[dict | None] = [None]

        def run_ph() -> None:
            try:
                ph_body[0] = _hash_via_sidecar(
                    cache_name, "PHASH", on_hasher_event=lambda e: hq.put(e)
                )
            except BaseException as e:  # noqa: BLE001
                ph_err[0] = e

        t_ph = threading.Thread(target=run_ph, daemon=True)
        t_ph.start()
        while t_ph.is_alive():
            try:
                ev = hq.get(timeout=0.2)
                yield json.dumps(ev, ensure_ascii=False) + "\n"
            except queue.Empty:
                pass
        t_ph.join()
        while not hq.empty():
            yield json.dumps(hq.get_nowait(), ensure_ascii=False) + "\n"
        if ph_err[0] is not None:
            raise ph_err[0]
        body = ph_body[0] or {}
        phash_hex = (body.get("hash") or "").strip().lower()
        if not phash_hex or len(phash_hex) != 16 or not re.fullmatch(r"[0-9a-f]{16}", phash_hex):
            raise RuntimeError("hasher returned no valid PHASH")

        osh_hash = ""
        if osh_remote.get("ok") and osh_remote.get("hash"):
            osh_hash = str(osh_remote["hash"]).strip().lower()
        if not osh_hash or len(osh_hash) != 16:
            yield json.dumps({"type": "phase", "phase": "hashing_oshash_file"}) + "\n"
            oq: queue.Queue = queue.Queue()
            os_err = [None]
            ob_holder: list[dict | None] = [None]

            def run_os() -> None:
                try:
                    ob_holder[0] = _hash_via_sidecar(
                        cache_name, "OSHASH", on_hasher_event=lambda e: oq.put(e)
                    )
                except BaseException as e:  # noqa: BLE001
                    os_err[0] = e

            t_os = threading.Thread(target=run_os, daemon=True)
            t_os.start()
            while t_os.is_alive():
                try:
                    ev = oq.get(timeout=0.2)
                    yield json.dumps(ev, ensure_ascii=False) + "\n"
                except queue.Empty:
                    pass
            t_os.join()
            while not oq.empty():
                yield json.dumps(oq.get_nowait(), ensure_ascii=False) + "\n"
            if os_err[0] is not None:
                raise os_err[0]
            ob = ob_holder[0] or {}
            osh_hash = (ob.get("hash") or "").strip().lower()

        size_b = os.path.getsize(os.path.join(DOWNLOADS_DIR, cache_name))
        dur_int = body.get("duration_int")
        if dur_int is None and osh_remote.get("ok"):
            dur_int = osh_remote.get("duration_int")
        if dur_int is not None:
            try:
                dur_int = int(dur_int)
            except (TypeError, ValueError):
                dur_int = None

        cache_fields = {
            "size_bytes": size_b,
            "oshash": osh_hash,
            "phash": phash_hex,
            "duration_int": dur_int,
        }
        if body.get("width") is not None:
            cache_fields["video_width"] = body.get("width")
        if body.get("height") is not None:
            cache_fields["video_height"] = body.get("height")
        _scenes_set(cache_name, **{k: v for k, v in cache_fields.items() if v is not None})

        payload: dict = {
            "ok": True,
            "url": url,
            "hash": phash_hex,
            "phash": phash_hex,
            "oshash": osh_hash,
            "cache_filename": cache_name,
            "size_bytes": size_b,
            "final_url": osh_remote.get("final_url") if osh_remote.get("ok") else None,
        }
        if dur_int is not None:
            payload["duration_int"] = dur_int
            payload["duration"] = float(dur_int)
        if osh_remote.get("ok") and osh_remote.get("duration_detail"):
            payload["duration_detail"] = osh_remote.get("duration_detail")

        if STASHDB_API_KEY:
            try:
                ordered = [("OSHASH", osh_hash), ("PHASH", phash_hex)]
                raw_scene, hit_algo = _stashdb_lookup_scene_ordered(ordered)
                if raw_scene:
                    payload["stashdb_match"] = _scene_fragment_to_match(raw_scene)
                    payload["stashdb_hit_by"] = hit_algo
                    if payload.get("duration_int") is None:
                        h_for_dur = osh_hash if hit_algo == "OSHASH" else phash_hex
                        dsec, dint, det = _stashdb_duration_fallback_from_fingerprint(
                            raw_scene, hit_algo, h_for_dur or ""
                        )
                        if dint is not None:
                            payload["duration"] = dsec
                            payload["duration_int"] = dint
                            payload["duration_detail"] = det
                else:
                    payload["stashdb_match"] = None
            except Exception as e:  # noqa: BLE001
                payload["stashdb_error"] = str(e)
        else:
            payload["stashdb_skipped"] = "STASHDB_API_KEY is not set"

        payload["stashdb_can_submit"] = bool(STASHDB_API_KEY and STASHDB_AUTO_CONTRIBUTE)
        yield json.dumps({"type": "done", **payload}, ensure_ascii=False, default=str) + "\n"
    except TransferCancelled:
        yield json.dumps({"type": "error", "ok": False, "error": "download cancelled"}) + "\n"
    except Exception as e:  # noqa: BLE001
        yield json.dumps({"type": "error", "ok": False, "error": str(e)}, ensure_ascii=False) + "\n"
    finally:
        _maybe_delete_remote_download(dl_path)


@app.route("/api/phash_from_url/stream", methods=["POST"])
def api_phash_from_url_stream():
    """Same as ``/api/phash_from_url`` but chunked NDJSON (download + hasher progress)."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400
    if not _hasher_service_active():
        return jsonify({"ok": False, "error": "Hasher is not configured"}), 503
    if not _hasher_algo_enabled("PHASH"):
        return jsonify({"ok": False, "error": "PHASH is disabled (HASHER_PHASH_ENABLED)"}), 403

    return Response(
        stream_with_context(_generate_url_fingerprint_job_ndjson(url)),
        mimetype="application/x-ndjson",
    )


@app.route("/api/remote_scenes_from_url/stream", methods=["POST"])
def api_remote_scenes_from_url_stream():
    """Same as ``/api/remote_scenes_from_url`` but NDJSON (download + hasher progress)."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400
    if not _hasher_service_active():
        return jsonify({"ok": False, "error": "Hasher is not configured"}), 503
    if not _hasher_algo_enabled("PHASH"):
        return jsonify({"ok": False, "error": "PHASH is disabled (HASHER_PHASH_ENABLED)"}), 403
    return Response(
        stream_with_context(_generate_url_fingerprint_job_ndjson(url)),
        mimetype="application/x-ndjson",
    )


@app.route("/api/hasher_hash_stream", methods=["POST"])
def api_hasher_hash_stream():
    """Proxy hasher-http ``/v1/hash_stream`` as NDJSON (for draft UI + tooling)."""
    if not _hasher_service_active():
        return jsonify({"error": "Hasher is disabled or HASHER_HTTP_URL is not set"}), 503
    data = request.get_json(silent=True) or {}
    raw = data.get("filename") or ""
    try:
        name = _safe_downloads_mount_rel(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    algorithm = (data.get("algorithm") or "PHASH").strip().upper()
    if algorithm not in HASHER_ALGORITHMS:
        return jsonify({"error": "unsupported algorithm", "supported": list(HASHER_ALGORITHMS)}), 400
    if not _hasher_algo_enabled(algorithm):
        return jsonify({"error": f"Algorithm {algorithm} is disabled"}), 403

    read_timeout = HASHER_HTTP_TIMEOUT if algorithm == "PHASH" else 300
    stream_url = f"{_hasher_base_url()}/v1/hash_stream"

    @stream_with_context
    def _proxy():
        try:
            with requests.post(
                stream_url,
                json={"filename": name, "algorithm": algorithm},
                headers=_hasher_headers(),
                timeout=(15, read_timeout),
                stream=True,
            ) as upstream:
                if upstream.status_code != 200:
                    yield json.dumps(
                        {
                            "type": "result",
                            "ok": False,
                            "error": "upstream_http",
                            "http_status": upstream.status_code,
                            "detail": (upstream.text or "")[:1200],
                        },
                        ensure_ascii=False,
                    ) + "\n"
                    return
                for line in upstream.iter_lines(decode_unicode=True):
                    if line:
                        yield line + "\n"
        except Exception as e:  # noqa: BLE001
            yield json.dumps(
                {"type": "result", "ok": False, "error": "proxy_failed", "detail": str(e)},
                ensure_ascii=False,
            ) + "\n"

    return Response(_proxy(), mimetype="application/x-ndjson")


@app.route("/api/remote_scenes_from_url", methods=["POST"])
def api_remote_scenes_from_url():
    """Remote OSHASH (Range) + full download + PHASH; StashDB; hashes.json."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400
    if not _hasher_service_active():
        return jsonify({"ok": False, "error": "Hasher is not configured"}), 503
    if not _hasher_algo_enabled("PHASH"):
        return jsonify({"ok": False, "error": "PHASH is disabled (HASHER_PHASH_ENABLED)"}), 403

    osh_remote = fetch_oshash_from_url(url)
    dl_path: str | None = None
    try:
        dl_path = download_file(url)
        cache_name = _remote_url_cache_filename(url)
        target = os.path.join(DOWNLOADS_DIR, cache_name)
        if dl_path != target:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
            shutil.move(dl_path, target)
            dl_path = target
        cache_name = os.path.basename(dl_path)

        ph_body = _hash_via_sidecar(cache_name, "PHASH")
        phash_hex = (ph_body.get("hash") or "").strip().lower()
        if not phash_hex or len(phash_hex) != 16 or not re.fullmatch(r"[0-9a-f]{16}", phash_hex):
            return jsonify({"ok": False, "error": "hasher returned no valid PHASH"}), 500

        osh_hash = ""
        if osh_remote.get("ok") and osh_remote.get("hash"):
            osh_hash = str(osh_remote["hash"]).strip().lower()
        if not osh_hash or len(osh_hash) != 16:
            ob = _hash_via_sidecar(cache_name, "OSHASH")
            osh_hash = (ob.get("hash") or "").strip().lower()

        size_b = os.path.getsize(os.path.join(DOWNLOADS_DIR, cache_name))
        dur_int = ph_body.get("duration_int")
        if dur_int is None and osh_remote.get("ok"):
            dur_int = osh_remote.get("duration_int")
        if dur_int is not None:
            try:
                dur_int = int(dur_int)
            except (TypeError, ValueError):
                dur_int = None

        cache_fields: dict = {
            "size_bytes": size_b,
            "oshash": osh_hash,
            "phash": phash_hex,
            "duration_int": dur_int,
        }
        if ph_body.get("width") is not None:
            cache_fields["video_width"] = ph_body.get("width")
        if ph_body.get("height") is not None:
            cache_fields["video_height"] = ph_body.get("height")
        _scenes_set(cache_name, **{k: v for k, v in cache_fields.items() if v is not None})

        ordered = [("OSHASH", osh_hash), ("PHASH", phash_hex)]

        payload: dict = {
            "ok": True,
            "url": url,
            "oshash": osh_hash,
            "phash": phash_hex,
            "cache_filename": cache_name,
            "size_bytes": size_b,
            "final_url": osh_remote.get("final_url") if osh_remote.get("ok") else None,
        }
        if dur_int is not None:
            payload["duration_int"] = dur_int
            payload["duration"] = float(dur_int)
        if osh_remote.get("ok") and osh_remote.get("duration_detail"):
            payload["duration_detail"] = osh_remote.get("duration_detail")

        if STASHDB_API_KEY:
            try:
                raw_scene, hit_algo = _stashdb_lookup_scene_ordered(ordered)
                if raw_scene:
                    payload["stashdb_match"] = _scene_fragment_to_match(raw_scene)
                    payload["stashdb_hit_by"] = hit_algo
                    if payload.get("duration_int") is None:
                        h_for_dur = osh_hash if hit_algo == "OSHASH" else phash_hex
                        dsec, dint, det = _stashdb_duration_fallback_from_fingerprint(
                            raw_scene, hit_algo, h_for_dur or ""
                        )
                        if dint is not None:
                            payload["duration"] = dsec
                            payload["duration_int"] = dint
                            payload["duration_detail"] = det
                else:
                    payload["stashdb_match"] = None
            except Exception as e:  # noqa: BLE001
                payload["stashdb_error"] = str(e)
        else:
            payload["stashdb_skipped"] = "STASHDB_API_KEY is not set"

        payload["stashdb_can_submit"] = bool(STASHDB_API_KEY and STASHDB_AUTO_CONTRIBUTE)
        return jsonify(payload)
    except TransferCancelled:
        return jsonify({"ok": False, "error": "download cancelled"}), 499
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _maybe_delete_remote_download(dl_path)


@app.route("/api/oshash_from_url", methods=["POST"])
def api_oshash_from_url():
    """Compute OSHASH via HTTP Range only; optional read-only StashDB lookup (no submit)."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    result = fetch_oshash_from_url(url)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("detail", "failed")}), 400

    payload = {
        "ok": True,
        "hash": result["hash"],
        "size_bytes": result["size_bytes"],
        "final_url": result.get("final_url"),
        "range_requests": result.get("range_requests"),
    }
    if result.get("duration") is not None:
        payload["duration"] = result["duration"]
    if result.get("duration_int") is not None:
        payload["duration_int"] = result["duration_int"]
    if result.get("duration_detail"):
        payload["duration_detail"] = result["duration_detail"]
    if STASHDB_API_KEY:
        try:
            matches = _stashdb_find_by_fingerprints(
                [{"algorithm": "OSHASH", "hash": result["hash"]}]
            )
            if matches:
                raw = matches[0]
                payload["stashdb_match"] = _scene_fragment_to_match(raw)
                if payload.get("duration_int") is None:
                    dsec, dint, det = _stashdb_duration_fallback_from_fingerprint(
                        raw, "OSHASH", result["hash"]
                    )
                    if dint is not None:
                        payload["duration"] = dsec
                        payload["duration_int"] = dint
                        payload["duration_detail"] = det
            else:
                payload["stashdb_match"] = None
        except Exception as e:  # noqa: BLE001
            payload["stashdb_error"] = str(e)
    else:
        payload["stashdb_skipped"] = "STASHDB_API_KEY is not set"

    payload["stashdb_can_submit"] = bool(STASHDB_API_KEY and STASHDB_AUTO_CONTRIBUTE)

    return jsonify(payload)


@app.route("/api/stashdb_submit_scene_fingerprints", methods=["POST"])
def api_stashdb_submit_scene_fingerprints():
    """Attach fingerprints to an existing scene (stash-box submitFingerprint).

    Intended for hashes computed outside the local file pipeline (e.g. remote
    OSHASH via HTTP Range).

    Body: scene_id (uuid), duration (seconds, int >= 0),
           fingerprints: [{ "algorithm": "OSHASH"|"PHASH", "hash": "..." }, ...]

    Requires STASHDB_API_KEY and STASHDB_AUTO_CONTRIBUTE enabled.
    """
    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set"}), 400
    if not STASHDB_AUTO_CONTRIBUTE:
        return jsonify({"error": "STASHDB_AUTO_CONTRIBUTE is disabled"}), 403

    data = request.get_json(silent=True) or {}
    scene_id = (data.get("scene_id") or "").strip()
    try:
        uuid.UUID(scene_id)
    except (ValueError, TypeError):
        return jsonify({"error": "scene_id must be a UUID"}), 400

    dur_raw = data.get("duration")
    try:
        duration_int = int(dur_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "duration must be an integer (seconds)"}), 400
    if duration_int < 0:
        return jsonify({"error": "duration must be >= 0"}), 400

    fps_in = data.get("fingerprints") or []
    pairs = []
    for fp in fps_in:
        if not isinstance(fp, dict):
            continue
        algo = (fp.get("algorithm") or "").strip().upper()
        h = (fp.get("hash") or "").strip()
        if algo in ("OSHASH", "PHASH"):
            h = re.sub(r"[^a-fA-F0-9]", "", h).lower()
        if algo not in ("OSHASH", "PHASH") or not h:
            continue
        if algo in ("OSHASH", "PHASH") and len(h) != 16:
            continue
        pairs.append((algo, h))

    if not pairs:
        return jsonify({
            "error": "Provide fingerprints: [{algorithm: OSHASH|PHASH, hash}]",
        }), 400

    results = []
    for algo, h in pairs:
        try:
            ok = _stashdb_submit_fingerprint(scene_id, algo, h, duration_int)
            results.append({"algorithm": algo, "hash": h, "ok": bool(ok)})
        except Exception as e:  # noqa: BLE001
            results.append({"algorithm": algo, "hash": h, "ok": False, "error": str(e)})

    return jsonify({"ok": True, "results": results})


@app.route("/api/stashdb_contribute_matched_scene", methods=["POST"])
def api_stashdb_contribute_matched_scene():
    """After a StashDB match, compute missing hashes and submit fingerprints.

    Body: filename (under /downloads), scene_id (uuid), matched_algo (OSHASH|PHASH),
          optional fingerprints: [{algorithm, hash}, ...], optional duration_int.
    """
    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set"}), 400
    if not STASHDB_AUTO_CONTRIBUTE:
        return jsonify({"error": "STASHDB_AUTO_CONTRIBUTE is disabled"}), 403

    data = request.get_json(silent=True) or {}
    raw = (data.get("filename") or "").strip()
    try:
        filename = _safe_downloads_mount_rel(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    scene_id = (data.get("scene_id") or "").strip()
    try:
        uuid.UUID(scene_id)
    except (ValueError, TypeError):
        return jsonify({"error": "scene_id must be a UUID"}), 400
    matched_algo = (data.get("matched_algo") or "OSHASH").strip().upper()
    fps_in = data.get("fingerprints") or []
    fingerprints_collected = []
    for fp in fps_in:
        if not isinstance(fp, dict):
            continue
        algo = (fp.get("algorithm") or "").strip().upper()
        h = (fp.get("hash") or "").strip()
        norm = _normalize_stashdb_fingerprint_hash(algo, h)
        if norm:
            fingerprints_collected.append({"algorithm": norm[0], "hash": norm[1]})

    dur_raw = data.get("duration_int")
    duration_int = None
    if dur_raw is not None:
        try:
            duration_int = int(dur_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "duration_int must be an integer"}), 400

    if not _hasher_service_active():
        return jsonify({
            "error": "Hasher is disabled (HASHER_ENABLED=0) or HASHER_HTTP_URL is not set.",
        }), 503

    parts = [p for p in filename.split("/") if p]
    full_path = os.path.normpath(os.path.join(DOWNLOADS_DIR, *parts))
    if not os.path.isfile(full_path):
        return jsonify({"error": f"File not found at {full_path}"}), 404

    ui = _contribute_fingerprints_for_matched_file(
        filename=filename,
        scene_id=scene_id,
        matched_algo=matched_algo,
        fingerprints_collected=fingerprints_collected,
        duration_int=duration_int,
    )
    if ui is None:
        return jsonify({"error": "Could not contribute fingerprints"}), 500
    return jsonify({"ok": True, "stashdb_contribute": ui})


# ── Shared API ───────────────────────────────────────────────────────


def _stashdb_extract_scene_id(raw_url):
    """UUID from a StashDB scene, edit, draft, or bare id string."""
    if not raw_url:
        return None
    text = raw_url.strip()
    if not text:
        return None
    match = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        text,
    )
    return match.group(0) if match else None


def _stashdb_url_kind(raw_url):
    """Classify StashDB link: scene, edit, or draft (defaults to scene)."""
    text = (raw_url or "").strip().lower()
    if not text:
        return None
    if re.search(r"/edits?/", text):
        return "edit"
    if re.search(r"/drafts?/", text):
        return "draft"
    if re.search(r"/scenes?/", text):
        return "scene"
    return "scene"


def _stashdb_pretty_date(raw_date):
    if not raw_date:
        return ""
    try:
        parsed = datetime.strptime(raw_date, "%Y-%m-%d")
        return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    except ValueError:
        return raw_date


# StashDB performer genders we *exclude* from autofill / helper UI.
# Defaults to MALE only — the user's autofill flow is for female-led scenes,
# but we keep TRANSGENDER_MALE etc. in by default since the cost of a wrong
# exclusion (a missing performer) is higher than including someone unintentionally.
# Centralised so the helper page, autofill, and post-upload check all agree.
EXCLUDED_GENDERS = {"MALE"}


def _stashdb_gender_value(gender_obj):
    """Normalize a StashDB Gender enum or wrapper to an uppercase string.

    StashDB exposes Gender as an enum whose values are strings like
    "MALE", "FEMALE", "TRANSGENDER_MALE", etc. We tolerate dicts in case a
    schema drift ever wraps it.
    """
    if isinstance(gender_obj, dict):
        v = gender_obj.get("name") or gender_obj.get("id")
        return str(v or "").strip().upper()
    return str(gender_obj or "").strip().upper()


def _is_excluded_performer(performer):
    """True iff this performer's gender is in EXCLUDED_GENDERS."""
    if not isinstance(performer, dict):
        return False
    return _stashdb_gender_value(performer.get("gender")) in EXCLUDED_GENDERS


def _stashdb_scene_lookup(scene_id):
    query = (
        """
    query SceneAutofill($id: ID!) {
      findScene(id: $id) {
        title
        date
        studio {
    """
        + _STUDIO_GRAPHQL_FRAGMENT
        + """
        }
        images {
          url
          width
          height
        }
        performers {
          performer {
            name
            gender
          }
        }
      }
    }
    """
    )
    headers = {"Content-Type": "application/json"}
    if STASHDB_API_KEY:
        headers["ApiKey"] = STASHDB_API_KEY

    r = requests.post(
        STASHDB_GRAPHQL_URL,
        json={"query": query, "variables": {"id": scene_id}},
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return (payload.get("data") or {}).get("findScene") or {}


def _stashdb_largest_image_url(scene):
    """Return URL of the largest scene image by pixel area, or None."""
    if not isinstance(scene, dict):
        return None
    best = None
    best_area = -1
    for img in scene.get("images") or []:
        if not isinstance(img, dict) or not img.get("url"):
            continue
        try:
            w = int(img.get("width") or 0)
            h = int(img.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        area = w * h
        if area > best_area:
            best_area = area
            best = (img.get("url") or "").strip()
    return best or None


_STASHDB_COVER_MAX_BYTES = 5 * 1024 * 1024
_STASHDB_SCENE_IMAGES_QUERY = """
query SceneImages($id: ID!) {
  findScene(id: $id) {
    id
    images { id url width height }
  }
}
"""


def _stashdb_largest_image_url_for_scene_id(scene_id: str) -> str | None:
    sid = (scene_id or "").strip()
    if not sid or not STASHDB_API_KEY:
        return None
    try:
        data = _stashdb_graphql(_STASHDB_SCENE_IMAGES_QUERY, {"id": sid})
    except Exception as exc:  # noqa: BLE001
        print(f"[STASHDB] scene images query failed for {sid}: {exc}", flush=True)
        return None
    scene = data.get("findScene")
    return _stashdb_largest_image_url(scene) if isinstance(scene, dict) else None


def _stashdb_cover_extension(url: str, content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    if ct in mapping:
        return mapping[ct]
    path = urllib.parse.urlparse(url).path
    suffix = os.path.splitext(path)[1].lower()
    if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        return suffix if suffix != ".jpeg" else ".jpg"
    guessed = mimetypes.guess_extension(ct or "")
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


def _stashdb_download_cover(scene_id: str, cache_dir: str) -> str | None:
    """Download the largest StashDB scene image for a folder thumbnail."""
    image_url = _stashdb_largest_image_url_for_scene_id(scene_id)
    if not image_url:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    try:
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("content-type")
    except requests.RequestException as exc:
        print(f"[STASHDB] cover download failed: {exc}", flush=True)
        return None
    if len(content) > _STASHDB_COVER_MAX_BYTES:
        print(
            f"[STASHDB] cover too large ({len(content):,} bytes); skipping thumbnail",
            flush=True,
        )
        return None
    ext = _stashdb_cover_extension(image_url, content_type)
    dest = os.path.join(cache_dir, f"{scene_id}{ext}")
    try:
        with open(dest, "wb") as fp:
            fp.write(content)
    except OSError as exc:
        print(f"[STASHDB] cover write failed: {exc}", flush=True)
        return None
    return dest


def _stashdb_autofill_payload_from_scene(scene):
    """BBCode autofill fields from a findScene-shaped dict (or synthetic merge)."""
    actresses = []
    for node in scene.get("performers") or []:
        if not isinstance(node, dict):
            continue
        performer = node.get("performer") if isinstance(node.get("performer"), dict) else node
        if not isinstance(performer, dict):
            continue
        if _is_excluded_performer(performer):
            continue
        name = (performer.get("name") or "").strip()
        if name:
            actresses.append(name)

    studio_name, network_name = _stashdb_studio_names(scene.get("studio"))

    cover_url = _stashdb_largest_image_url(scene)
    image_bbcode = f"[IMG]{cover_url}[/IMG]" if cover_url else ""

    return {
        "actresses": actresses,
        "title": (scene.get("title") or "").strip(),
        "upload_date": _stashdb_pretty_date((scene.get("date") or "").strip()),
        "studio": studio_name,
        "network_studio": network_name or "",
        "image_bbcode": image_bbcode,
    }


def _stashdb_graphql(query, variables=None, timeout=20):
    headers = {"Content-Type": "application/json"}
    if STASHDB_API_KEY:
        headers["ApiKey"] = STASHDB_API_KEY
    r = requests.post(
        STASHDB_GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return payload.get("data") or {}


_SCENE_EDIT_AUTOFRAGMENT = (
    """
        title
        details
        director
        code
        date
        urls { url }
        studio {
    """
    + _STUDIO_GRAPHQL_FRAGMENT
    + """
        }
        images { url width height }
        performers {
          performer { name gender }
        }
"""
)


def _stashdb_edit_lookup(edit_id):
    query = (
        """
    query EditAutofill($id: ID!) {
      findEdit(id: $id) {
        id
        operation
        status
        target_type
        target {
          __typename
          ... on Scene {
            id
            title
            details
            director
            code
            date
            urls { url }
            studio {
"""
        + _STUDIO_GRAPHQL_FRAGMENT
        + """
            }
            images { url width height }
            performers { performer { name gender } }
          }
        }
        details {
          __typename
          ... on SceneEdit {
"""
        + _SCENE_EDIT_AUTOFRAGMENT
        + """
          }
        }
        old_details {
          __typename
          ... on SceneEdit {
"""
        + _SCENE_EDIT_AUTOFRAGMENT
        + """
          }
        }
      }
    }
    """
    )
    return (_stashdb_graphql(query, {"id": edit_id}).get("findEdit") or None)


def _stashdb_draft_lookup(draft_id):
    query = (
        """
    query DraftAutofill($id: ID!) {
      findDraft(id: $id) {
        id
        data {
          __typename
          ... on SceneDraft {
            title
            details
            director
            code
            date
            urls
            studio {
    """
        + _STUDIO_GRAPHQL_FRAGMENT
        + """
            }
            image { url width height }
            performers {
              __typename
              ... on Performer { name gender }
              ... on DraftEntity { name }
            }
          }
        }
      }
    }
    """
    )
    return (_stashdb_graphql(query, {"id": draft_id}).get("findDraft") or None)


def _stashdb_scene_like_from_edit(edit):
    """Merge SceneEdit details with existing Scene target for MODIFY edits."""
    details = edit.get("details") if isinstance(edit.get("details"), dict) else {}
    if details.get("__typename") != "SceneEdit":
        return None
    target = edit.get("target") if isinstance(edit.get("target"), dict) else {}

    def pick(field):
        val = details.get(field)
        if val is not None and val != "":
            return val
        return target.get(field)

    scene = {
        "title": pick("title") or "",
        "details": pick("details") or "",
        "director": pick("director") or "",
        "code": pick("code") or "",
        "date": pick("date") or "",
    }

    studio = details.get("studio") if isinstance(details.get("studio"), dict) else None
    if not studio or not studio.get("name"):
        studio = target.get("studio") if isinstance(target.get("studio"), dict) else None
    scene["studio"] = studio

    performers = details.get("performers")
    if not performers:
        performers = target.get("performers")
    scene["performers"] = performers or []

    images = details.get("images")
    if not images:
        images = target.get("images")
    scene["images"] = images or []

    return scene


def _stashdb_scene_like_from_draft(draft):
    data = draft.get("data") if isinstance(draft.get("data"), dict) else {}
    if data.get("__typename") != "SceneDraft":
        return None

    performers = []
    for node in data.get("performers") or []:
        if not isinstance(node, dict):
            continue
        if node.get("__typename") == "DraftEntity":
            performers.append({"performer": {"name": node.get("name"), "gender": None}})
        elif node.get("name"):
            performers.append({"performer": node})

    images = []
    img = data.get("image")
    if isinstance(img, dict) and img.get("url"):
        images.append(img)

    urls = []
    for u in data.get("urls") or []:
        if isinstance(u, str) and u.strip():
            urls.append({"url": u.strip()})
        elif isinstance(u, dict) and u.get("url"):
            urls.append({"url": u["url"]})

    return {
        "title": data.get("title") or "",
        "details": data.get("details") or "",
        "director": data.get("director") or "",
        "code": data.get("code") or "",
        "date": data.get("date") or "",
        "studio": data.get("studio"),
        "performers": performers,
        "images": images,
        "urls": urls,
    }


@app.route("/api/stashdb_autofill", methods=["POST"])
def api_stashdb_autofill():
    """Autofill BBCode fields from a StashDB scene, pending edit, or draft (GraphQL + API key)."""
    data = request.get_json(silent=True) or {}
    raw_url = (data.get("url") or "").strip()
    if not raw_url:
        return jsonify({"error": "StashDB URL is required"}), 400

    resource_id = _stashdb_extract_scene_id(raw_url)
    if not resource_id:
        return jsonify({"error": "Could not detect a StashDB id in the URL"}), 400
    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set in container environment"}), 400

    kind = _stashdb_url_kind(raw_url)
    source_meta = {"source": kind, "id": resource_id}

    try:
        if kind == "edit":
            edit = _stashdb_edit_lookup(resource_id)
            if not edit:
                return jsonify({"error": "Edit not found on StashDB"}), 404
            if (edit.get("target_type") or "").upper() != "SCENE":
                return jsonify({"error": "Only scene edits are supported for autofill"}), 400
            scene = _stashdb_scene_like_from_edit(edit)
            if not scene:
                return jsonify({"error": "Edit has no scene details"}), 400
            target = edit.get("target") if isinstance(edit.get("target"), dict) else {}
            if target.get("id"):
                source_meta["scene_id"] = target["id"]
                source_meta["scene_url"] = f"{STASHDB_SCENES_BASE}/{target['id']}"
            source_meta["edit_id"] = resource_id
            source_meta["edit_status"] = edit.get("status")
            source_meta["edit_operation"] = edit.get("operation")
        elif kind == "draft":
            draft = _stashdb_draft_lookup(resource_id)
            if not draft:
                return jsonify({"error": "Draft not found on StashDB (or not visible to your API key)"}), 404
            scene = _stashdb_scene_like_from_draft(draft)
            if not scene:
                return jsonify({"error": "Only scene drafts are supported for autofill"}), 400
            source_meta["draft_id"] = resource_id
        else:
            scene = _stashdb_scene_lookup(resource_id)
            if not scene:
                return jsonify({"error": "Scene not found on StashDB"}), 404
            source_meta["scene_id"] = resource_id
            source_meta["scene_url"] = f"{STASHDB_SCENES_BASE}/{resource_id}"
    except Exception as e:
        detail = str(e)
        if isinstance(e, requests.HTTPError) and e.response is not None:
            body = (e.response.text or "").strip()
            if body:
                detail = f"{detail} | body: {body[:600]}"
        return jsonify({"error": f"Failed to fetch from StashDB: {detail}"}), 502

    payload = _stashdb_autofill_payload_from_scene(scene)
    payload.update(source_meta)

    try:
        filename = _safe_downloads_mount_rel((data.get("filename") or "").strip())
    except ValueError:
        filename = ""
    has_local_file = bool(filename)

    network = (payload.get("network_studio") or "").strip() or None
    studio_out, gallery_url, studio_meta = _resolve_studio_for_autofill(
        payload.get("studio") or "", network
    )
    payload["studio"] = studio_out
    if studio_meta.get("alias_applied"):
        payload["studio_alias"] = studio_meta
    elif studio_meta.get("network_studio"):
        payload["studio_network"] = studio_meta
    if gallery_url and not (data.get("skip_gallery") or False) and GOFILE_ENABLED:
        payload["gallery_link"] = gallery_url
        payload["gallery_match"] = studio_meta.get("folder_match")
        payload["gallery_matched_level"] = studio_meta.get("matched_level")
    fs_gallery = studio_meta.get("filester_gallery_link")
    if fs_gallery and not (data.get("skip_gallery") or False):
        payload["filester_gallery_link"] = fs_gallery
    thread_url = studio_meta.get("thread_url")
    if thread_url:
        payload["thread_url"] = thread_url
    link_label = (studio_meta.get("studio_link_label") or "").strip()
    if link_label:
        payload["studio_link_label"] = link_label

    return jsonify(payload)


# ── StashDB draft submit / duplicate-check helpers ───────────────────

def _stashdb_headers():
    h = {"Content-Type": "application/json"}
    if STASHDB_API_KEY:
        h["ApiKey"] = STASHDB_API_KEY
    return h


def _stashdb_post(query, variables=None, timeout=20):
    r = requests.post(
        STASHDB_GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers=_stashdb_headers(),
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return payload.get("data") or {}


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_url(raw):
    if not raw or not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError:
        return text.lower()
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        scheme = "https"
    host = (parts.hostname or "").lower()
    if not host:
        return text.lower()
    path = (parts.path or "").rstrip("/")
    return urlunsplit((scheme, host, path, "", ""))


def _coerce_url_list(values):
    if not values:
        return []
    if isinstance(values, str):
        chunks = re.split(r"[\s,;\n]+", values)
    elif isinstance(values, list):
        chunks = values
    else:
        return []
    out = []
    for v in chunks:
        if not isinstance(v, str):
            continue
        s = v.strip()
        if s:
            out.append(s)
    return out


def _stashdb_search_scene(term):
    """Returns a list of scene fragments via stash-box searchScene."""
    if not term:
        return []
    query = """
    query SearchScene($term: String!) {
      searchScene(term: $term) {
        id
        title
        date
        urls {
          url
        }
        studio {
          name
        }
        performers {
          performer {
            name
            gender
          }
        }
      }
    }
    """
    data = _stashdb_post(query, {"term": term}, timeout=25)
    return data.get("searchScene") or []


def _stashdb_find_by_fingerprints(fingerprints):
    """fingerprints: [{algorithm, hash}] -> list of matches.
    Note: stash-box FingerprintQueryInput is hash+algorithm only; duration is
    only used in FingerprintInput (draft submission)."""
    if not fingerprints:
        return []
    query_objs = []
    for fp in fingerprints:
        if not isinstance(fp, dict):
            continue
        algo = (fp.get("algorithm") or "").strip().upper()
        h = (fp.get("hash") or "").strip()
        if algo and h:
            query_objs.append({"algorithm": algo, "hash": h})
    if not query_objs:
        return []
    query = """
    query FindScenesBySceneFingerprints($fingerprints: [[FingerprintQueryInput!]!]!) {
      findScenesBySceneFingerprints(fingerprints: $fingerprints) {
        id
        title
        date
        duration
        urls {
          url
        }
        studio {
          name
        }
        performers {
          performer {
            name
            gender
          }
        }
        fingerprints {
          algorithm
          hash
          duration
        }
      }
    }
    """
    data = _stashdb_post(query, {"fingerprints": [query_objs]}, timeout=25)
    raw = data.get("findScenesBySceneFingerprints")
    if not raw:
        return []
    if raw and isinstance(raw[0], list):
        flat = []
        seen = set()
        for sub in raw:
            for item in (sub or []):
                if isinstance(item, dict) and item.get("id") not in seen:
                    seen.add(item.get("id"))
                    flat.append(item)
        return flat
    return raw


def _stashdb_duration_fallback_from_fingerprint(raw_scene: dict, algorithm: str, hash_hex: str):
    """Prefer matching fingerprint row duration on StashDB; else scene-level duration."""
    want_algo = (algorithm or "").strip().upper()
    want_h = (hash_hex or "").strip().lower()
    if want_algo and want_h:
        for fp in raw_scene.get("fingerprints") or []:
            if not isinstance(fp, dict):
                continue
            algo = str(fp.get("algorithm") or "").strip().upper()
            if algo != want_algo:
                continue
            h = str(fp.get("hash") or "").strip().lower()
            if h != want_h:
                continue
            dur = fp.get("duration")
            if dur is None:
                continue
            try:
                di = int(dur)
                if di >= 0:
                    tag = f"stashdb_fingerprint_{want_algo.lower()}"
                    return float(di), di, tag
            except (TypeError, ValueError):
                continue

    sd = raw_scene.get("duration")
    if sd is not None:
        try:
            di = int(sd)
            if di >= 0:
                return float(di), di, "stashdb_scene_duration"
        except (TypeError, ValueError):
            pass
    return None, None, None


def _stashdb_duration_fallback_from_match(raw_scene: dict, oshash_hex: str):
    """Reuse StashDB duration when local MP4 mvhd parse failed (OSHASH row)."""
    return _stashdb_duration_fallback_from_fingerprint(raw_scene, "OSHASH", oshash_hex)


def _scene_fragment_to_match(scene):
    if not isinstance(scene, dict):
        return None
    sid = scene.get("id") or ""
    urls = []
    for u in scene.get("urls") or []:
        if isinstance(u, dict) and u.get("url"):
            urls.append(u["url"])
    studio = ""
    if isinstance(scene.get("studio"), dict):
        studio = (scene["studio"].get("name") or "").strip()
    performers = []
    for node in scene.get("performers") or []:
        if not isinstance(node, dict):
            continue
        p = node.get("performer") if isinstance(node.get("performer"), dict) else node
        if not isinstance(p, dict):
            continue
        if _is_excluded_performer(p):
            continue
        name = (p.get("name") or "").strip()
        if name:
            performers.append(name)
    return {
        "id": sid,
        "title": (scene.get("title") or "").strip(),
        "date": scene.get("date") or "",
        "studio": studio,
        "performers": performers,
        "urls": urls,
        "link": f"{STASHDB_SCENES_BASE}/{sid}" if sid else "",
    }


def _stashdb_scene_full_query(scene_id):
    """Enriched single-scene fetch used by /bbcode and the PNG endpoint.

    Returns the raw GraphQL fragment (or None). Includes performers' gender,
    StashDB profile URLs, and the scene image set so the helper page can pick a
    cover image. Server filters performers via EXCLUDED_GENDERS at the call
    site (we keep the raw fragment around for the PNG endpoint that needs the
    image list regardless of gender filter)."""
    if not scene_id:
        return None
    query = (
        """
    query SceneFull($id: ID!) {
      findScene(id: $id) {
        id
        title
        release_date
        urls {
          url
        }
        studio {
          urls {
            url
          }
    """
        + _STUDIO_GRAPHQL_FRAGMENT
        + """
        }
        images {
          id
          url
          width
          height
        }
        performers {
          performer {
            id
            name
            gender
            urls {
              url
            }
          }
        }
      }
    }
    """
    )
    data = _stashdb_post(query, {"id": scene_id}, timeout=20)
    return data.get("findScene")


def _scene_fragment_to_full(scene):
    """Trim a SceneFull fragment for the helper page (drops MALE performers,
    flattens URL lists)."""
    if not isinstance(scene, dict):
        return None
    sid = scene.get("id") or ""
    urls = []
    for u in scene.get("urls") or []:
        if isinstance(u, dict) and u.get("url"):
            urls.append(u["url"])
    studio = None
    studio_obj = scene.get("studio")
    if isinstance(studio_obj, dict):
        studio_urls = []
        for u in studio_obj.get("urls") or []:
            if isinstance(u, dict) and u.get("url"):
                studio_urls.append(u["url"])
        parent = None
        par = studio_obj.get("parent")
        if isinstance(par, dict) and (par.get("name") or "").strip():
            parent = {
                "id": par.get("id"),
                "name": (par.get("name") or "").strip(),
            }
        studio = {
            "name": (studio_obj.get("name") or "").strip(),
            "urls": studio_urls,
            "parent": parent,
        }
    images = []
    for img in scene.get("images") or []:
        if not isinstance(img, dict) or not img.get("url"):
            continue
        images.append({
            "id": img.get("id"),
            "url": img.get("url"),
            "width": img.get("width") or 0,
            "height": img.get("height") or 0,
        })
    images.sort(key=lambda i: (i.get("width") or 0) * (i.get("height") or 0), reverse=True)
    performers = []
    for node in scene.get("performers") or []:
        if not isinstance(node, dict):
            continue
        p = node.get("performer") if isinstance(node.get("performer"), dict) else node
        if not isinstance(p, dict):
            continue
        if _is_excluded_performer(p):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        # Never surface StashDB performer profile URLs to the BBCode helper —
        # the user fills SimpCity (or other) links manually.
        performers.append({
            "id": p.get("id"),
            "name": name,
            "gender": _stashdb_gender_value(p.get("gender")),
            "urls": [],
        })
    return {
        "id": sid,
        "title": (scene.get("title") or "").strip(),
        "release_date": scene.get("release_date") or "",
        "urls": urls,
        "studio": studio,
        "images": images,
        "performers": performers,
        "link": f"{STASHDB_SCENES_BASE}/{sid}" if sid else "",
    }


# ── hasher-http client ───────────────────────────────────────────────

def _hasher_headers():
    h = {"Content-Type": "application/json"}
    if HASHER_HTTP_TOKEN:
        h["Authorization"] = f"Bearer {HASHER_HTTP_TOKEN}"
    return h


def _hasher_base_url() -> str:
    return (HASHER_HTTP_URL or "").rstrip("/")


def _hasher_call(filename, algorithm, on_event=None):
    """Run one OSHASH | PHASH against the hasher-http sidecar.

    When ``on_event`` is set, uses ``/v1/hash_stream`` (NDJSON) and invokes
    ``on_event(dict)`` for each event; returns the final result body (without
    the wrapping ``type: result`` key).

    When ``on_event`` is None, uses the single-shot ``/v1/hash`` JSON endpoint.
    """
    if not HASHER_HTTP_URL:
        raise RuntimeError("HASHER_HTTP_URL is not configured")
    algorithm = (algorithm or "PHASH").strip().upper()
    if algorithm not in HASHER_ALGORITHMS:
        raise ValueError(f"unsupported algorithm {algorithm!r}")
    if not _hasher_algo_enabled(algorithm):
        raise RuntimeError(
            f"Hasher algorithm {algorithm} is disabled (HASHER_ENABLED or HASHER_*_ENABLED)"
        )
    read_timeout = HASHER_HTTP_TIMEOUT if algorithm == "PHASH" else 300

    if on_event is None:
        url = f"{_hasher_base_url()}/v1/hash"
        r = requests.post(
            url,
            json={"filename": filename, "algorithm": algorithm},
            headers=_hasher_headers(),
            timeout=(15, read_timeout),
        )
        try:
            body = r.json()
        except ValueError:
            body = {"raw": (r.text or "")[:500]}
        if r.status_code == 404:
            raise FileNotFoundError(body.get("detail") or body.get("error") or "file_not_found")
        if r.status_code == 503:
            raise RuntimeError("hasher-http busy; try again shortly")
        if r.status_code >= 400 or not body.get("ok"):
            err = body.get("error") or body
            det = body.get("detail")
            if det:
                err = f"{err}: {det}"
            raise RuntimeError(f"hasher-http {r.status_code} {algorithm}: {err}")
        return body

    stream_url = f"{_hasher_base_url()}/v1/hash_stream"
    with requests.post(
        stream_url,
        json={"filename": filename, "algorithm": algorithm},
        headers=_hasher_headers(),
        timeout=(15, read_timeout),
        stream=True,
    ) as r:
        if r.status_code != 200:
            text = (r.text or "")[:1200]
            raise RuntimeError(f"hasher-http stream HTTP {r.status_code}: {text}")
        result = None
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if on_event:
                on_event(ev)
            if ev.get("type") == "result":
                result = ev
        if not result:
            raise RuntimeError("hasher stream ended without a result line")
        if not result.get("ok"):
            err = result.get("error") or result.get("detail") or result
            if result.get("error") == "file_not_found":
                raise FileNotFoundError(str(err))
            if result.get("error") == "busy":
                raise RuntimeError("hasher-http busy; try again shortly")
            raise RuntimeError(f"hasher-http {algorithm}: {err}")
        return {k: v for k, v in result.items() if k != "type"}


def _safe_downloads_mount_rel(raw: str) -> str:
    """Basename or ``subdir/name`` under the downloads mount (POSIX ``/`` segments, no ``..``)."""
    if not raw or not isinstance(raw, str):
        raise ValueError("filename is required")
    if "\x00" in raw:
        raise ValueError("invalid filename")
    normalized = raw.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/"):
        raise ValueError("invalid filename")
    parts = [p for p in normalized.split("/") if p and p != "."]
    if not parts or ".." in parts:
        raise ValueError("invalid filename")
    return "/".join(parts)


def _thumb_sheet_basename(video_key: str) -> str:
    """Relative path of the PNG grid under THUMBS_DIR (matches thumber_http / thumbs.sh)."""
    vk = _safe_downloads_mount_rel(video_key)
    base = posixpath.basename(vk)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    d = posixpath.dirname(vk)
    if d and d != ".":
        return f"{d}/{stem}_thumbs.png"
    return f"{stem}_thumbs.png"


def _thumb_sheet_path(video_key: str) -> str | None:
    """Absolute path to thumbnail sheet if it exists under THUMBS_DIR, else None."""
    try:
        sheet_name = _thumb_sheet_basename(video_key)
    except ValueError:
        return None
    parts = [p for p in sheet_name.split("/") if p and p != "."]
    if not parts or ".." in parts:
        return None
    base = os.path.realpath(THUMBS_DIR)
    try:
        full = os.path.realpath(os.path.join(base, *parts))
    except OSError:
        return None
    try:
        if os.path.commonpath([full, base]) != base:
            return None
    except ValueError:
        return None
    if not os.path.isfile(full):
        return None
    return full


# ── StashDB endpoints ────────────────────────────────────────────────

def _hash_via_sidecar(name, algorithm, on_hasher_event=None):
    """Shared helper for the apu-side hash endpoints. Returns the hasher-http
    body or raises the same exceptions _hasher_call does."""
    if not _hasher_service_active():
        raise RuntimeError(
            "Hasher is disabled (HASHER_ENABLED=0) or HASHER_HTTP_URL is not set."
        )
    rel = _safe_downloads_mount_rel(name)
    parts = [p for p in rel.split("/") if p]
    full_path = os.path.normpath(os.path.join(DOWNLOADS_DIR, *parts))
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"File not found at {full_path}")
    return _hasher_call(rel, algorithm, on_event=on_hasher_event)


@app.route("/api/stashdb_video_hash", methods=["POST"])
def api_stashdb_video_hash():
    """Generic hash endpoint. Body: { filename, algorithm: OSHASH|PHASH }.
    OSHASH typically finishes in under a few seconds; PHASH is the slow one."""
    data = request.get_json(silent=True) or {}
    raw = data.get("filename") or ""
    try:
        name = _safe_downloads_mount_rel(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    algorithm = (data.get("algorithm") or "PHASH").strip().upper()
    if algorithm not in HASHER_ALGORITHMS:
        return jsonify({
            "error": "unsupported algorithm",
            "supported": list(HASHER_ALGORITHMS),
        }), 400

    if not _hasher_service_active():
        return jsonify({"error": "Hasher is disabled or HASHER_HTTP_URL is not set"}), 503
    if not _hasher_algo_enabled(algorithm):
        return jsonify({
            "error": f"Algorithm {algorithm} is disabled (HASHER_*_ENABLED).",
        }), 403

    try:
        body = _hash_via_sidecar(name, algorithm)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except requests.RequestException as e:
        return jsonify({"error": f"hasher-http unreachable: {e}"}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "filename": name,
        "algorithm": algorithm,
        "hash": body.get("hash"),
        "duration": body.get("duration"),
        "duration_int": body.get("duration_int"),
        "width": body.get("width"),
        "height": body.get("height"),
    })


@app.route("/api/stashdb_video_fingerprint", methods=["POST"])
def api_stashdb_video_fingerprint():
    """Backward-compat alias: same as /api/stashdb_video_hash with algorithm=PHASH.
    Response also exposes the result under the legacy `phash` key."""
    data = request.get_json(silent=True) or {}
    raw = data.get("filename") or ""
    try:
        name = _safe_downloads_mount_rel(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not _hasher_service_active():
        return jsonify({"error": "Hasher is disabled or HASHER_HTTP_URL is not set"}), 503
    if not _hasher_algo_enabled("PHASH"):
        return jsonify({"error": "PHASH is disabled (HASHER_PHASH_ENABLED=0)"}), 403

    try:
        body = _hash_via_sidecar(name, "PHASH")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except requests.RequestException as e:
        return jsonify({"error": f"hasher-http unreachable: {e}"}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500

    h = body.get("hash")
    return jsonify({
        "ok": True,
        "filename": name,
        "phash": h,
        "hash": h,
        "algorithm": "PHASH",
        "duration": body.get("duration"),
        "duration_int": body.get("duration_int"),
        "width": body.get("width"),
        "height": body.get("height"),
    })


@app.route("/api/stashdb_find_by_fingerprint", methods=["POST"])
def api_stashdb_find_by_fingerprint():
    """Cheap single-fingerprint StashDB lookup used by the auto-check UI.
    Body: { algorithm, hash }. Returns { matched, matches[] }."""
    data = request.get_json(silent=True) or {}
    algorithm = (data.get("algorithm") or "").strip().upper()
    h = (data.get("hash") or "").strip()
    if algorithm not in HASHER_ALGORITHMS:
        return jsonify({
            "error": "unsupported algorithm",
            "supported": list(HASHER_ALGORITHMS),
        }), 400
    if not h:
        return jsonify({"error": "hash is required"}), 400
    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set"}), 400

    try:
        results = _stashdb_find_by_fingerprints([{"algorithm": algorithm, "hash": h}])
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        return jsonify({"error": f"StashDB HTTP error: {e} | {body[:400]}"}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"findScenesBySceneFingerprints failed: {e}"}), 502

    matches = []
    for s in results or []:
        m = _scene_fragment_to_match(s)
        if m:
            matches.append(m)

    return jsonify({
        "ok": True,
        "algorithm": algorithm,
        "hash": h,
        "matched": bool(matches),
        "matches": matches[:5],
    })


def _build_search_term(title, studio, performers):
    parts = [title or ""]
    if studio:
        parts.append(studio)
    if performers:
        parts.extend([p for p in performers if p])
    term = " ".join(p.strip() for p in parts if p and p.strip())
    return term[:150]


@app.route("/api/stashdb_check_scene", methods=["POST"])
def api_stashdb_check_scene():
    """Pre-submit duplicate detection.
    Inputs: title, studio, performers[], urls[], fingerprints[]
    Output: likely_matches (search-based), fingerprint_matches, can_submit_safely."""
    data = request.get_json(silent=True) or {}

    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set"}), 400

    title = (data.get("title") or "").strip()
    studio = (data.get("studio") or "").strip()
    performers = [s.strip() for s in (data.get("performers") or []) if isinstance(s, str) and s.strip()]
    urls = _coerce_url_list(data.get("urls"))
    fingerprints_in = data.get("fingerprints") or []

    norm_user_urls = {_normalize_url(u) for u in urls}
    norm_user_urls.discard("")

    fingerprints = []
    for fp in fingerprints_in:
        if not isinstance(fp, dict):
            continue
        algo = (fp.get("algorithm") or "").strip().upper()
        h = (fp.get("hash") or "").strip()
        dur = fp.get("duration")
        try:
            dur_int = int(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur_int = None
        if algo and h:
            fp_obj = {"algorithm": algo, "hash": h}
            if dur_int is not None:
                fp_obj["duration"] = dur_int
            fingerprints.append(fp_obj)

    likely = []
    fp_matches = []
    errors = []

    if title or studio or performers:
        try:
            term = _build_search_term(title, studio, performers)
            if term:
                results = _stashdb_search_scene(term)
                for s in results or []:
                    m = _scene_fragment_to_match(s)
                    if not m:
                        continue
                    norm_match_urls = {_normalize_url(u) for u in m["urls"]}
                    norm_match_urls.discard("")
                    m["url_overlap"] = sorted(norm_user_urls & norm_match_urls)
                    likely.append(m)
        except Exception as e:  # noqa: BLE001
            errors.append(f"searchScene: {e}")

    if fingerprints:
        try:
            results = _stashdb_find_by_fingerprints(fingerprints)
            for s in results or []:
                m = _scene_fragment_to_match(s)
                if m:
                    fp_matches.append(m)
        except Exception as e:  # noqa: BLE001
            errors.append(f"findScenesBySceneFingerprints: {e}")

    has_url_overlap = any(m.get("url_overlap") for m in likely)
    has_fp_match = bool(fp_matches)
    can_submit_safely = not (has_url_overlap or has_fp_match)

    return jsonify({
        "ok": True,
        "term_used": _build_search_term(title, studio, performers),
        "likely_matches": likely[:20],
        "fingerprint_matches": fp_matches[:20],
        "has_url_overlap": has_url_overlap,
        "has_fingerprint_match": has_fp_match,
        "can_submit_safely": can_submit_safely,
        "errors": errors,
    })


def _stashdb_submit_scene_draft(payload):
    """Send the SubmitSceneDraft mutation. payload is a dict matching SceneDraftInput."""
    mutation = """
    mutation SubmitSceneDraft($input: SceneDraftInput!) {
      submitSceneDraft(input: $input) {
        id
      }
    }
    """
    data = _stashdb_post(mutation, {"input": payload}, timeout=30)
    res = (data.get("submitSceneDraft") or {}).get("id")
    return res


def _stashdb_submit_fingerprint(scene_id, algorithm, hash_value, duration):
    """Attach a fingerprint to an existing scene (stash-box submitFingerprint)."""
    norm = _normalize_stashdb_fingerprint_hash(algorithm, hash_value)
    if not norm:
        raise ValueError(f"invalid {algorithm} fingerprint hash")
    algo, h = norm
    mutation = """
    mutation SubmitFingerprint($input: FingerprintSubmission!) {
      submitFingerprint(input: $input)
    }
    """
    variables = {
        "input": {
            "scene_id": scene_id,
            "fingerprint": {
                "algorithm": algo,
                "hash": h,
                "duration": int(duration),
            },
        }
    }
    data = _stashdb_post(mutation, variables, timeout=20)
    return bool(data.get("submitFingerprint"))


@app.route("/api/stashdb_submit_scene_draft", methods=["POST"])
def api_stashdb_submit_scene_draft():
    """Build SceneDraftInput from manual fields and submit a draft to StashDB.
    Requires explicit override flag if the prior /api/stashdb_check_scene call
    flagged duplicates."""
    data = request.get_json(silent=True) or {}

    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set"}), 400

    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    date = (data.get("date") or "").strip()
    if date and not _DATE_RE.match(date):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    studio = (data.get("studio") or "").strip()
    details = (data.get("details") or "").strip()
    director = (data.get("director") or "").strip()
    code = (data.get("code") or "").strip()
    urls = _coerce_url_list(data.get("urls"))
    performers = [s.strip() for s in (data.get("performers") or []) if isinstance(s, str) and s.strip()]
    fingerprints_in = data.get("fingerprints") or []
    confirmed = bool(data.get("confirm_override"))

    fingerprints = []
    for fp in fingerprints_in:
        if not isinstance(fp, dict):
            continue
        algo = (fp.get("algorithm") or "").strip().upper()
        h = (fp.get("hash") or "").strip()
        dur = fp.get("duration")
        try:
            dur_int = int(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur_int = None
        norm = _normalize_stashdb_fingerprint_hash(algo, h)
        if norm and dur_int is not None:
            algo, h = norm
            fingerprints.append({"algorithm": algo, "hash": h, "duration": dur_int})

    # Server-side guardrail: re-check duplicates unless caller passed confirm_override.
    duplicate_blocked = False
    duplicate_payload = None
    if not confirmed:
        try:
            check_resp = api_stashdb_check_scene_internal(
                title=title, studio=studio, performers=performers,
                urls=urls, fingerprints=fingerprints,
            )
            if not check_resp["can_submit_safely"]:
                duplicate_blocked = True
                duplicate_payload = check_resp
        except Exception as e:  # noqa: BLE001
            print(f"[STASHDB] pre-submit check failed (continuing): {e}", flush=True)

    if duplicate_blocked:
        return jsonify({
            "error": "Likely duplicate detected. Re-submit with confirm_override=true to bypass.",
            "duplicate_check": duplicate_payload,
        }), 409

    scene_input = {"title": title}
    if date:
        scene_input["date"] = date
    if details:
        scene_input["details"] = details
    if director:
        scene_input["director"] = director
    if code:
        scene_input["code"] = code
    if urls:
        scene_input["urls"] = urls
    if studio:
        scene_input["studio"] = {"name": studio}
    if performers:
        scene_input["performers"] = [{"name": n} for n in performers]
    if fingerprints:
        scene_input["fingerprints"] = fingerprints

    try:
        draft_id = _stashdb_submit_scene_draft(scene_input)
    except requests.HTTPError as e:
        body = ""
        if e.response is not None:
            body = (e.response.text or "")[:600]
        return jsonify({"error": f"submitSceneDraft HTTP error: {e} | {body}"}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"submitSceneDraft failed: {e}"}), 502

    if not draft_id:
        return jsonify({"error": "StashDB returned no draft id"}), 502

    return jsonify({
        "ok": True,
        "draft_id": draft_id,
        "draft_url": f"{STASHDB_DRAFTS_BASE}/{draft_id}",
    })


def _build_scene_draft_input(*, title, date, details, director, code, studio,
                              urls, performers, fingerprints):
    scene_input = {"title": title}
    if date:
        scene_input["date"] = date
    if details:
        scene_input["details"] = details
    if director:
        scene_input["director"] = director
    if code:
        scene_input["code"] = code
    if urls:
        scene_input["urls"] = urls
    if studio:
        scene_input["studio"] = {"name": studio}
    if performers:
        scene_input["performers"] = [{"name": n} for n in performers]
    if fingerprints:
        scene_input["fingerprints"] = fingerprints
    return scene_input


@app.route("/api/stashdb_auto_submit", methods=["POST"])
def api_stashdb_auto_submit():
    """End-to-end pipeline:

      1. Compute OSHASH  → query StashDB; if match, stop (no draft).
      2. Compute PHASH   → query StashDB; if match, stop (no draft).  (skip with skip_phash=true)
      3. Optional URL/title overlap check                              (skip with skip_url_check=true)
      4. Submit draft with all collected fingerprints (needs duration; needs title).

    The point: OSHASH is essentially free vs PHASH (single-digit seconds vs
    20-30 seconds), and *most* files in a batch are already on StashDB. This short-circuits
    the slow PHASH step on the common case while still falling back to it for novel files.

    Body fields:
      filename               (required) path under /downloads (basename or ``subdir/name.mp4``)
      title, studio, urls, performers, details, director, code, date    (draft metadata)
      skip_phash             (default false) — useful for batch quick-pass
      skip_url_check         (default false) — skip step 4
      confirm_override       (default false) — submit draft even on URL overlap
      dry_run                (default false) — never submit; report what would happen

    Response (always JSON):
      result: "matched" | "drafted" | "no_match" | "missing_metadata" | "error"
      algorithm?, scene?, draft_id?, draft_url?
      fingerprints[], duration_int?, steps[], errors[]
    """
    data = request.get_json(silent=True) or {}
    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set"}), 400

    raw = data.get("filename") or ""
    try:
        filename = _safe_downloads_mount_rel(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not _hasher_service_active():
        return jsonify({
            "error": "Hasher is disabled (HASHER_ENABLED=0) or HASHER_HTTP_URL is not set.",
        }), 503

    parts = [p for p in filename.split("/") if p]
    full_path = os.path.normpath(os.path.join(DOWNLOADS_DIR, *parts))
    if not os.path.isfile(full_path):
        return jsonify({"error": f"File not found at {full_path}"}), 404

    skip_phash = bool(data.get("skip_phash"))
    skip_url_check = bool(data.get("skip_url_check"))
    confirm_override = bool(data.get("confirm_override"))
    dry_run = bool(data.get("dry_run"))

    title = (data.get("title") or "").strip()
    studio = (data.get("studio") or "").strip()
    urls = _coerce_url_list(data.get("urls"))
    performers = [s.strip() for s in (data.get("performers") or [])
                  if isinstance(s, str) and s.strip()]
    details = (data.get("details") or "").strip()
    director = (data.get("director") or "").strip()
    code = (data.get("code") or "").strip()
    date = (data.get("date") or "").strip()
    if date and not _DATE_RE.match(date):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    steps = []
    errors = []
    fingerprints_collected = []
    duration_int = None

    algorithms = []
    for a in ("OSHASH", "PHASH"):
        if not _hasher_algo_enabled(a):
            continue
        if a == "PHASH" and skip_phash:
            continue
        algorithms.append(a)
    if not algorithms:
        return jsonify({
            "error": "No hasher algorithms enabled (HASHER_*_ENABLED) or PHASH skipped.",
        }), 503

    for algo in algorithms:
        step = {"step": algo, "status": "computing"}
        t0 = time.monotonic()
        try:
            hresp = _hash_via_sidecar(filename, algo)
        except FileNotFoundError as e:
            return jsonify({
                "result": "error",
                "error": f"file not found: {e}",
                "steps": steps + [{"step": algo, "status": "error", "detail": str(e)}],
                "errors": errors,
            }), 404
        except RuntimeError as e:
            return jsonify({
                "result": "error",
                "error": str(e),
                "steps": steps + [{"step": algo, "status": "error", "detail": str(e)}],
                "errors": errors,
            }), 503
        except requests.RequestException as e:
            return jsonify({
                "result": "error",
                "error": f"hasher-http unreachable: {e}",
                "steps": steps + [{"step": algo, "status": "error", "detail": str(e)}],
                "errors": errors,
            }), 502
        except Exception as e:  # noqa: BLE001
            step["status"] = "compute_failed"
            step["detail"] = str(e)
            step["elapsed_sec"] = round(time.monotonic() - t0, 2)
            steps.append(step)
            errors.append(f"{algo} compute: {e}")
            continue

        elapsed = round(time.monotonic() - t0, 2)
        h = hresp.get("hash")
        duration_int = duration_int or hresp.get("duration_int")
        step["hash"] = h
        step["elapsed_sec"] = elapsed
        if not h:
            step["status"] = "compute_failed"
            steps.append(step)
            errors.append(f"{algo}: no hash returned")
            continue

        fingerprints_collected.append({"algorithm": algo, "hash": h})

        try:
            results = _stashdb_find_by_fingerprints([{"algorithm": algo, "hash": h}])
        except Exception as e:  # noqa: BLE001
            step["status"] = "check_failed"
            step["detail"] = str(e)
            steps.append(step)
            errors.append(f"{algo} stashdb check: {e}")
            continue

        if results:
            m = _scene_fragment_to_match(results[0])
            raw_scene = results[0]
            step["status"] = "match"
            step["match"] = m
            steps.append(step)
            contribute = _contribute_fingerprints_for_matched_file(
                filename=filename,
                scene_id=m.get("id") or "",
                matched_algo=algo,
                fingerprints_collected=fingerprints_collected,
                duration_int=duration_int,
                raw_scene=raw_scene,
            )
            return jsonify({
                "result": "matched",
                "algorithm": algo,
                "scene": m,
                "fingerprints": fingerprints_collected,
                "duration_int": duration_int,
                "steps": steps,
                "errors": errors,
                "stashdb_contribute": contribute,
            })

        step["status"] = "miss"
        steps.append(step)

    # Hash check came up empty. Optional URL/title belt-and-suspenders pass.
    if not skip_url_check and (title or studio or urls or performers):
        try:
            check = api_stashdb_check_scene_internal(
                title=title, studio=studio, performers=performers,
                urls=urls, fingerprints=[],
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"url_check: {e}")
            check = None
        if check is not None:
            url_step = {"step": "URL", "status": "miss",
                        "term_used": check.get("term_used")}
            if check.get("has_url_overlap") and not confirm_override:
                url_step["status"] = "match"
                url_step["matches"] = check.get("likely_matches") or []
                steps.append(url_step)
                return jsonify({
                    "result": "matched",
                    "algorithm": "URL",
                    "duplicate_check": check,
                    "fingerprints": fingerprints_collected,
                    "duration_int": duration_int,
                    "steps": steps,
                    "errors": errors + (check.get("errors") or []),
                })
            steps.append(url_step)

    if dry_run:
        return jsonify({
            "result": "no_match",
            "fingerprints": fingerprints_collected,
            "duration_int": duration_int,
            "steps": steps,
            "errors": errors,
        })

    if not title:
        return jsonify({
            "result": "missing_metadata",
            "error": "Title is required to submit a draft (no metadata = nothing to draft).",
            "fingerprints": fingerprints_collected,
            "duration_int": duration_int,
            "steps": steps,
            "errors": errors,
        }), 400

    if duration_int is None:
        return jsonify({
            "result": "missing_metadata",
            "error": "Could not determine video duration; aborting draft.",
            "fingerprints": fingerprints_collected,
            "steps": steps,
            "errors": errors,
        }), 400

    fingerprints_for_draft = [
        {"algorithm": fp["algorithm"], "hash": fp["hash"], "duration": duration_int}
        for fp in fingerprints_collected
    ]
    scene_input = _build_scene_draft_input(
        title=title, date=date, details=details, director=director, code=code,
        studio=studio, urls=urls, performers=performers,
        fingerprints=fingerprints_for_draft,
    )

    try:
        draft_id = _stashdb_submit_scene_draft(scene_input)
    except requests.HTTPError as e:
        body = ""
        if e.response is not None:
            body = (e.response.text or "")[:600]
        return jsonify({
            "result": "error",
            "error": f"submitSceneDraft HTTP error: {e} | {body}",
            "fingerprints": fingerprints_collected,
            "duration_int": duration_int,
            "steps": steps,
            "errors": errors,
        }), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({
            "result": "error",
            "error": f"submitSceneDraft failed: {e}",
            "fingerprints": fingerprints_collected,
            "duration_int": duration_int,
            "steps": steps,
            "errors": errors,
        }), 502

    if not draft_id:
        return jsonify({
            "result": "error",
            "error": "StashDB returned no draft id",
            "fingerprints": fingerprints_collected,
            "duration_int": duration_int,
            "steps": steps,
            "errors": errors,
        }), 502

    steps.append({"step": "DRAFT", "status": "submitted", "draft_id": draft_id})
    return jsonify({
        "result": "drafted",
        "draft_id": draft_id,
        "draft_url": f"{STASHDB_DRAFTS_BASE}/{draft_id}",
        "fingerprints": fingerprints_collected,
        "duration_int": duration_int,
        "steps": steps,
        "errors": errors,
    })


def api_stashdb_check_scene_internal(*, title, studio, performers, urls, fingerprints):
    """Same logic as /api/stashdb_check_scene but reusable from other endpoints
    without going through Flask's request context."""
    norm_user_urls = {_normalize_url(u) for u in urls}
    norm_user_urls.discard("")

    likely = []
    fp_matches = []
    errors = []

    if title or studio or performers:
        try:
            term = _build_search_term(title, studio, performers)
            if term:
                results = _stashdb_search_scene(term)
                for s in results or []:
                    m = _scene_fragment_to_match(s)
                    if not m:
                        continue
                    norm_match_urls = {_normalize_url(u) for u in m["urls"]}
                    norm_match_urls.discard("")
                    m["url_overlap"] = sorted(norm_user_urls & norm_match_urls)
                    likely.append(m)
        except Exception as e:  # noqa: BLE001
            errors.append(f"searchScene: {e}")

    if fingerprints:
        try:
            results = _stashdb_find_by_fingerprints(fingerprints)
            for s in results or []:
                m = _scene_fragment_to_match(s)
                if m:
                    fp_matches.append(m)
        except Exception as e:  # noqa: BLE001
            errors.append(f"findScenesBySceneFingerprints: {e}")

    has_url_overlap = any(m.get("url_overlap") for m in likely)
    has_fp_match = bool(fp_matches)
    return {
        "term_used": _build_search_term(title, studio, performers),
        "likely_matches": likely[:20],
        "fingerprint_matches": fp_matches[:20],
        "has_url_overlap": has_url_overlap,
        "has_fingerprint_match": has_fp_match,
        "can_submit_safely": not (has_url_overlap or has_fp_match),
        "errors": errors,
    }


# ── Post-upload StashDB auto-check ─────────────────────────────────
#
# Called from the upload _worker (parallel postprocess thread) alongside GoFile upload.
#
#   stashdb_match:        { scene_id, scene_url, matched_by, title, ... }
#   stashdb_helper_url:   "/bbcode?..."  (if matched)
#                      or "/stashdb-scene-draft?filename=..." (no match)
#   stashdb_contribute:   { message, submitted[{algorithm,hash}], ... }
#   stashdb_check_error:  "..." string  (only set when something failed; the
#                                        upload still proceeds normally)
#   Optional PHASH submit after match: HASHER_PHASH_ENABLED + STASHDB_AUTO_CONTRIBUTE (no UI toggle).


def _normalize_stashdb_fingerprint_hash(algorithm: str, hash_value: str) -> tuple[str, str] | None:
    """Return (ALGO, normalized_hash) or None if invalid."""
    algo = (algorithm or "").strip().upper()
    h = re.sub(r"[^a-fA-F0-9]", "", str(hash_value or "")).lower()
    if algo in ("OSHASH", "PHASH"):
        if len(h) != 16:
            return None
    else:
        return None
    if not h:
        return None
    return algo, h


def _resolve_contribute_duration(
    entry: dict,
    *,
    raw_scene: dict | None = None,
    matched_algo: str | None = None,
) -> int | None:
    """Best-effort duration (seconds) for submitFingerprint."""
    dur = entry.get("duration_int")
    if dur is not None:
        try:
            di = int(dur)
            if di >= 0:
                return di
        except (TypeError, ValueError):
            pass
    if not isinstance(raw_scene, dict):
        return None
    algo = (matched_algo or "").strip().upper()
    h = entry.get(algo.lower()) if algo else None
    if algo and h:
        _, di, _ = _stashdb_duration_fallback_from_fingerprint(raw_scene, algo, h)
        if di is not None:
            return di
    if entry.get("oshash"):
        _, di, _ = _stashdb_duration_fallback_from_fingerprint(
            raw_scene, "OSHASH", entry["oshash"]
        )
        if di is not None:
            return di
  # scene-level duration as last resort
    sd = raw_scene.get("duration")
    if sd is not None:
        try:
            di = int(sd)
            if di >= 0:
                return di
        except (TypeError, ValueError):
            pass
    return None


def _ensure_entry_media_and_hashes(
    job_id,
    filename: str,
    entry: dict,
    *,
    matched_algo: str | None = None,
) -> None:
    """Fill duration in ``entry`` when missing (mutates in place)."""
    log = lambda msg: _append_hasher_log(job_id, msg) if job_id else None

    needs_probe = (
        entry.get("duration_int") is None
        or entry.get("video_width") is None
        or entry.get("video_height") is None
    )
    if needs_probe and _hasher_algo_enabled("OSHASH"):
        try:
            if log:
                log("probing media info via OSHASH")
            body = _hash_via_sidecar(filename, "OSHASH")
            if body.get("width") is not None:
                entry["video_width"] = body.get("width")
            if body.get("height") is not None:
                entry["video_height"] = body.get("height")
            if not entry.get("oshash") and body.get("hash"):
                entry["oshash"] = body["hash"]
            if entry.get("duration_int") is None and body.get("duration_int") is not None:
                entry["duration_int"] = body.get("duration_int")
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"media probe skipped: {e}")


def _contribute_fingerprints_for_matched_file(
    *,
    filename: str,
    scene_id: str,
    matched_algo: str,
    fingerprints_collected=None,
    duration_int=None,
    raw_scene=None,
    job_id=None,
):
    """Compute missing hashes and submit fingerprints to an existing matched scene."""
    if not scene_id or not STASHDB_API_KEY or not STASHDB_AUTO_CONTRIBUTE:
        return None
    try:
        parts = [p for p in filename.split("/") if p]
        full_path = os.path.normpath(os.path.join(DOWNLOADS_DIR, *parts))
        size = os.path.getsize(full_path)
    except OSError:
        return None

    entry = {"size_bytes": size}
    if duration_int is not None:
        entry["duration_int"] = duration_int
    for fp in fingerprints_collected or []:
        if isinstance(fp, dict) and fp.get("algorithm") and fp.get("hash"):
            entry[str(fp["algorithm"]).lower()] = fp["hash"]
    cached = _scenes_get(filename, expected_size=size) or {}
    for key in ("oshash", "phash", "duration_int", "video_width", "video_height"):
        if entry.get(key) is None and cached.get(key) is not None:
            entry[key] = cached[key]

    _ensure_entry_media_and_hashes(
        job_id, filename, entry, matched_algo=matched_algo,
    )
    _scenes_set(filename, **{
        k: v for k, v in entry.items()
        if k in ("size_bytes", "duration_int", "oshash", "phash",
                 "video_width", "video_height")
    })
    fragment, ui = _stashdb_contribute_after_match(
        job_id,
        filename,
        scene_id,
        matched_algo,
        entry,
        size,
        raw_scene=raw_scene,
    )
    _scenes_set(filename, stashdb=fragment)
    return ui


def _stashdb_contribute_after_match(
    job_id,
    filename,
    scene_id,
    matched_algo,
    entry,
    size_bytes,
    *,
    raw_scene=None,
):
    """After a match, best-effort submitFingerprint for every hash already in `entry`.

    Merges `contributed` in scenes.json for idempotency. Never raises.
    Returns (stashdb_fragment_for_scenes_json, ui_summary_dict).
    """
    checked_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    cached = _scenes_get(filename, expected_size=size_bytes) or {}
    prev_sd = cached.get("stashdb") if isinstance(cached.get("stashdb"), dict) else {}
    contributed = list(prev_sd.get("contributed") or [])
    if prev_sd.get("scene_id") != scene_id:
        contributed = []
    contrib_set = set(contributed)

    submitted = []
    skipped_already = []
    rejected_false = []
    failed = []

    def _fp_label(algo: str, h: str) -> str:
        return f"{algo} {h}"

    duration_int = _resolve_contribute_duration(
        entry, raw_scene=raw_scene, matched_algo=matched_algo
    )
    if duration_int is not None and entry.get("duration_int") is None:
        entry["duration_int"] = duration_int
    if STASHDB_AUTO_CONTRIBUTE and duration_int is None:
        if job_id:
            _append_hasher_log(job_id, "stashdb contribute skipped: no duration")

    if STASHDB_AUTO_CONTRIBUTE and duration_int is not None and scene_id:
        for algo in _helper_stashdb_lookup_algos():
            algo_key = algo.lower()
            h = entry.get(algo_key)
            if not h:
                continue
            norm = _normalize_stashdb_fingerprint_hash(algo, h)
            if not norm:
                failed.append({"algorithm": algo, "hash": h, "detail": "invalid hash format"})
                if job_id:
                    _append_hasher_log(job_id, f"stashdb contribute {algo}: invalid hash format")
                continue
            algo, h = norm
            fp_key = f"{algo}:{h}"
            fp_row = {"algorithm": algo, "hash": h}
            if fp_key in contrib_set:
                skipped_already.append(fp_row)
                continue
            try:
                ok = _stashdb_submit_fingerprint(scene_id, algo, h, duration_int)
                if ok:
                    contrib_set.add(fp_key)
                    submitted.append(fp_row)
                    if job_id:
                        _append_hasher_log(job_id, f"stashdb contributed {algo} {h}")
                else:
                    rejected_false.append(fp_row)
                    if job_id:
                        _append_hasher_log(job_id, f"stashdb contribute {algo}: stash-box returned false")
            except Exception as e:  # noqa: BLE001
                failed.append({"algorithm": algo, "hash": h, "detail": str(e)})
                if job_id:
                    _append_hasher_log(job_id, f"stashdb contribute {algo}: {e}")

    contributed_sorted = sorted(contrib_set)

    # One-line summary for the upload job UI (poll endpoint).
    parts_msg = []
    if not STASHDB_AUTO_CONTRIBUTE:
        parts_msg.append("Automatic StashDB fingerprint submission is off (STASHDB_AUTO_CONTRIBUTE).")
    elif not scene_id:
        parts_msg.append("Fingerprint submission skipped (no scene id).")
    elif not duration_int:
        parts_msg.append("Fingerprint submission skipped (video duration unknown).")
    elif submitted:
        parts_msg.append(
            "Submitted to StashDB: "
            + ", ".join(_fp_label(x["algorithm"], x["hash"]) for x in submitted)
            + "."
        )
    if skipped_already:
        parts_msg.append(
            "Already on file for this scene (skipped): "
            + ", ".join(_fp_label(x["algorithm"], x["hash"]) for x in skipped_already)
            + "."
        )
    if rejected_false:
        parts_msg.append(
            "StashDB declined: "
            + ", ".join(_fp_label(x["algorithm"], x["hash"]) for x in rejected_false)
            + "."
        )
    if failed:
        for f in failed:
            fp = _fp_label(f["algorithm"], f.get("hash") or "")
            parts_msg.append(
                f"{fp} failed: {f['detail'][:120]}"
                + ("…" if len(f["detail"]) > 120 else "")
            )

    stashdb_fragment = {
        "scene_id": scene_id,
        "matched_by": matched_algo,
        "checked_at": checked_at,
        "contributed": contributed_sorted,
    }
    ui_summary = {
        "enabled": STASHDB_AUTO_CONTRIBUTE,
        "submitted": submitted,
        "skipped_already": skipped_already,
        "rejected_false": rejected_false,
        "failed": failed,
        "skipped_no_duration": bool(STASHDB_AUTO_CONTRIBUTE and not duration_int),
        "skipped_no_scene_id": bool(STASHDB_AUTO_CONTRIBUTE and not scene_id),
        "message": " ".join(parts_msg).strip(),
    }
    return stashdb_fragment, ui_summary


def _stashdb_contribute_merge_delta(
    job_id,
    *,
    submitted=None,
    skipped_already=None,
    rejected_false=None,
    failed=None,
    message_suffix="",
):
    """Append fingerprint rows / message into job['stashdb_contribute'] (optional PHASH follow-up)."""
    job = jobs.get(job_id)
    if not job:
        return
    sc = job.get("stashdb_contribute")
    if not isinstance(sc, dict):
        sc = {}
        job["stashdb_contribute"] = sc
    if submitted:
        sc.setdefault("submitted", []).extend(submitted)
    if skipped_already:
        sc.setdefault("skipped_already", []).extend(skipped_already)
    if rejected_false:
        sc.setdefault("rejected_false", []).extend(rejected_false)
    if failed:
        sc.setdefault("failed", []).extend(failed)
    if message_suffix:
        prev = (sc.get("message") or "").strip()
        sc["message"] = (prev + (" · " if prev else "") + message_suffix).strip()


def _stashdb_phash_followup(job_id, downloaded_path):
    """When HASHER_PHASH_ENABLED: compute PHASH if needed and submit to StashDB for matched scenes."""
    job = jobs.get(job_id)
    if not job or not _hasher_algo_enabled("PHASH"):
        return
    mp = job.get("stashdb_match")
    if not mp:
        return
    if HASHER_PHASH_ONLY_ON_MISS:
        matched_by = (mp.get("matched_by") or "").strip().upper()
        if matched_by == "OSHASH":
            _stashdb_contribute_merge_delta(
                job_id,
                message_suffix=(
                    f"Optional PHASH: skipped (matched by {matched_by}; "
                    "HASHER_PHASH_ONLY_ON_MISS=1)."
                ),
            )
            return
    scene_id = (mp.get("scene_id") or "").strip()
    if not scene_id:
        _stashdb_contribute_merge_delta(
            job_id,
            message_suffix="Optional PHASH: skipped (no scene id).",
        )
        return
    if not _hasher_service_active():
        _stashdb_contribute_merge_delta(
            job_id,
            message_suffix="Optional PHASH: hasher not configured.",
        )
        return

    filename = _downloads_key_for_sidecars(downloaded_path)
    try:
        size = os.path.getsize(downloaded_path)
    except OSError as e:
        _stashdb_contribute_merge_delta(
            job_id,
            message_suffix=f"Optional PHASH: could not stat file ({e}).",
        )
        return

    entry = _scenes_get(filename, expected_size=size) or {}
    if not entry.get("phash"):
        try:
            _append_hasher_log(job_id, "optional PHASH: computing")
            ph_hook = _make_hasher_stream_progress_callback(job_id, "PHASH")
            body = _hash_via_sidecar(filename, "PHASH", on_hasher_event=ph_hook)
            ph = body.get("hash")
            if ph:
                entry["phash"] = ph
                if entry.get("duration_int") is None and body.get("duration_int") is not None:
                    entry["duration_int"] = body.get("duration_int")
                if body.get("width") is not None:
                    entry["video_width"] = body.get("width")
                if body.get("height") is not None:
                    entry["video_height"] = body.get("height")
                _scenes_set(filename, **{
                    k: v for k, v in entry.items()
                    if k in ("size_bytes", "duration_int", "oshash", "phash",
                             "video_width", "video_height")
                })
                entry = _scenes_get(filename, expected_size=size) or entry
                _append_hasher_log(job_id, f"optional PHASH: {ph}")
            else:
                _stashdb_contribute_merge_delta(
                    job_id,
                    message_suffix="Optional PHASH: hasher returned no PHASH.",
                )
                return
        except Exception as e:  # noqa: BLE001
            _append_hasher_log(job_id, f"optional PHASH compute failed: {e}")
            _stashdb_contribute_merge_delta(
                job_id,
                message_suffix=f"Optional PHASH compute failed: {e}",
            )
            return

    h = entry.get("phash")
    if not h:
        return

    duration_int = entry.get("duration_int")
    if not duration_int:
        _stashdb_contribute_merge_delta(
            job_id,
            message_suffix="Optional PHASH: duration unknown; cannot submit to StashDB.",
        )
        return

    if not STASHDB_AUTO_CONTRIBUTE:
        _stashdb_contribute_merge_delta(
            job_id,
            message_suffix=(
                "Optional PHASH: computed and cached; enable STASHDB_AUTO_CONTRIBUTE to submit."
            ),
        )
        return
    if not STASHDB_API_KEY:
        _stashdb_contribute_merge_delta(
            job_id,
            message_suffix="Optional PHASH: computed; STASHDB_API_KEY missing for submission.",
        )
        return

    cached = _scenes_get(filename, expected_size=size) or {}
    prev_sd = cached.get("stashdb") if isinstance(cached.get("stashdb"), dict) else {}
    contributed = list(prev_sd.get("contributed") or [])
    if prev_sd.get("scene_id") != scene_id:
        contributed = []
    contrib_set = set(contributed)
    algo = "PHASH"
    fp_key = f"{algo}:{h}"
    fp_row = {"algorithm": algo, "hash": h}

    if fp_key in contrib_set:
        _stashdb_contribute_merge_delta(
            job_id,
            skipped_already=[fp_row],
            message_suffix=(
                "Optional PHASH: already contributed for this scene "
                f"({algo} {h})."
            ),
        )
        return

    try:
        ok = _stashdb_submit_fingerprint(scene_id, algo, h, duration_int)
        if ok:
            contrib_set.add(fp_key)
            stash_prev = dict(prev_sd)
            stash_prev["scene_id"] = scene_id
            stash_prev["matched_by"] = mp.get("matched_by") or stash_prev.get("matched_by")
            stash_prev["checked_at"] = stash_prev.get("checked_at") or datetime.utcnow().strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            stash_prev["contributed"] = sorted(contrib_set)
            _scenes_set(filename, stashdb=stash_prev)
            _stashdb_contribute_merge_delta(
                job_id,
                submitted=[fp_row],
                message_suffix=f"Optional PHASH: submitted {algo} {h}.",
            )
            _append_hasher_log(job_id, f"optional PHASH submitted {h}")
        else:
            _stashdb_contribute_merge_delta(
                job_id,
                rejected_false=[fp_row],
                message_suffix=f"Optional PHASH: StashDB declined {algo} {h}.",
            )
    except Exception as e:  # noqa: BLE001
        _append_hasher_log(job_id, f"optional PHASH submit failed: {e}")
        _stashdb_contribute_merge_delta(
            job_id,
            failed=[{"algorithm": algo, "hash": h, "detail": str(e)}],
            message_suffix=f"Optional PHASH submit failed: {e}",
        )


def _stashdb_post_upload_check(job_id, downloaded_path, *, parallel_with_upload: bool = False):
    """OSHASH → PHASH short-circuit lookup against StashDB.

    Uses the local hash cache (size-keyed) so files we've already seen don't
    pay the compute cost again. Sets the helper-URL fields on the job and
    returns the cached entry (or None on hard failure)."""

    if not downloaded_path or not os.path.isfile(downloaded_path):
        return None
    if not _is_video_file(downloaded_path):
        return None

    job = jobs.get(job_id)
    if job is None:
        return None
    if not STASHDB_API_KEY:
        return None

    filename = _downloads_key_for_sidecars(downloaded_path)

    if not _hasher_service_active():
        quoted = urllib.parse.quote(filename, safe="")
        job["stashdb_match"] = None
        job["stashdb_contribute"] = None
        job["stashdb_helper_url"] = f"/stashdb-scene-draft?filename={quoted}"
        _append_hasher_log(
            job_id,
            "StashDB fingerprint scan skipped (HASHER_ENABLED=0 or HASHER_HTTP_URL unset)",
        )
        return None

    _append_hasher_log(job_id, f"starting StashDB check for {filename}")
    try:
        size = os.path.getsize(downloaded_path)
    except OSError as e:
        job["stashdb_check_error"] = f"could not stat file: {e}"
        return None

    cached = _scenes_get(filename, expected_size=size) or {}
    entry = {"size_bytes": size}
    entry.update({k: v for k, v in cached.items() if k in
                  ("oshash", "phash", "duration_int",
                   "video_width", "video_height")})

    # Cached hashes may lack ffprobe duration/dimensions. One cheap OSHASH call
    # fills them for BBCode helper + submitFingerprint without redoing PHASH.
    if (
        entry.get("duration_int") is None
        or entry.get("video_width") is None
        or entry.get("video_height") is None
    ) and _hasher_algo_enabled("OSHASH"):
        try:
            _append_hasher_log(job_id, "media probe via OSHASH")
            dim_body = _hash_via_sidecar(filename, "OSHASH")
            if dim_body.get("width") is not None:
                entry["video_width"] = dim_body.get("width")
            if dim_body.get("height") is not None:
                entry["video_height"] = dim_body.get("height")
            if not entry.get("oshash") and dim_body.get("hash"):
                entry["oshash"] = dim_body["hash"]
            if entry.get("duration_int") is None and dim_body.get("duration_int") is not None:
                entry["duration_int"] = dim_body.get("duration_int")
            _scenes_set(filename, **{
                k: v for k, v in entry.items()
                if k in ("size_bytes", "duration_int", "oshash", "phash",
                         "video_width", "video_height")
            })
        except Exception as e:  # noqa: BLE001
            print(f"[STASHDB] optional dimension probe failed: {e}", flush=True)
            _append_hasher_log(job_id, f"dimension probe skipped: {e}")

    if not parallel_with_upload:
        job["status_text"] = "Checking StashDB..."

    matched_scene = None
    matched_algo = None
    raw_matched_scene = None

    try:
        for algo in _helper_stashdb_lookup_algos():
            algo_key = algo.lower()
            h = entry.get(algo_key)
            if not h:
                _append_hasher_log(job_id, f"{algo}: computing")
                ph_hook = _make_hasher_stream_progress_callback(job_id, algo) if algo == "PHASH" else None
                try:
                    body = _hash_via_sidecar(filename, algo, on_hasher_event=ph_hook)
                finally:
                    if algo == "PHASH":
                        j = jobs.get(job_id)
                        if j:
                            j.pop("hash_progress", None)
                h = body.get("hash")
                entry[algo_key] = h
                if entry.get("duration_int") is None and body.get("duration_int") is not None:
                    entry["duration_int"] = body.get("duration_int")
                if body.get("width") is not None:
                    entry["video_width"] = body.get("width")
                if body.get("height") is not None:
                    entry["video_height"] = body.get("height")
                # Persist after every successful compute so partial progress survives.
                _scenes_set(filename, **{
                    k: v for k, v in entry.items()
                    if k in ("size_bytes", "duration_int", "oshash", "phash",
                             "video_width", "video_height")
                })
            else:
                _append_hasher_log(job_id, f"{algo}: using cached hash")
            if not h:
                continue
            try:
                results = _stashdb_find_by_fingerprints([{"algorithm": algo, "hash": h}])
            except Exception as e:  # noqa: BLE001
                job["stashdb_check_error"] = f"StashDB {algo} lookup failed: {e}"
                _append_hasher_log(job_id, f"{algo}: lookup error: {e}")
                continue
            if results:
                matched_scene = _scene_fragment_to_match(results[0])
                raw_matched_scene = results[0]
                matched_algo = algo
                _append_hasher_log(job_id, f"{algo}: matched scene {matched_scene.get('id')}")
                break
            _append_hasher_log(job_id, f"{algo}: no match")
    except FileNotFoundError as e:
        job["stashdb_check_error"] = f"file not found in hasher mount: {e}"
        _append_hasher_log(job_id, f"error: {job['stashdb_check_error']}")
    except RuntimeError as e:
        job["stashdb_check_error"] = f"hasher unreachable: {e}"
        _append_hasher_log(job_id, f"error: {job['stashdb_check_error']}")
    except requests.RequestException as e:
        job["stashdb_check_error"] = f"hasher HTTP error: {e}"
        _append_hasher_log(job_id, f"error: {job['stashdb_check_error']}")
    except Exception as e:  # noqa: BLE001
        job["stashdb_check_error"] = f"hash check failed: {e}"
        _append_hasher_log(job_id, f"error: {job['stashdb_check_error']}")

    if matched_scene and matched_algo:
        _ensure_entry_media_and_hashes(
            job_id, filename, entry, matched_algo=matched_algo,
        )

    # Whatever we computed, flush the latest into the cache.
    _scenes_set(filename, **{
        k: v for k, v in entry.items()
        if k in ("size_bytes", "duration_int", "oshash", "phash",
                 "video_width", "video_height")
    })

    quoted = urllib.parse.quote(filename, safe="")

    if matched_scene and matched_algo:
        match_payload = {
            "scene_id": matched_scene.get("id"),
            "scene_url": matched_scene.get("link"),
            "matched_by": matched_algo,
            "title": matched_scene.get("title"),
            "studio": matched_scene.get("studio"),
            "date": matched_scene.get("date"),
            "performers": matched_scene.get("performers") or [],
        }
        job["stashdb_match"] = match_payload
        cover_path = _stashdb_download_cover(
            matched_scene.get("id") or "",
            os.path.join(HASHES_DIR, "stashdb-covers"),
        )
        if cover_path:
            job["stashdb_cover_path"] = cover_path
            _append_hasher_log(job_id, "StashDB cover cached for folder thumbnail")
        else:
            job.pop("stashdb_cover_path", None)
        _maybe_prepare_filester_split_folder(job_id)
        job["stashdb_helper_url"] = _bbcode_helper_url(
            matched_scene["id"], filename, _scenes_get(filename, expected_size=size) or entry
        )
        stashdb_fragment, contrib_ui = _stashdb_contribute_after_match(
            job_id,
            filename,
            matched_scene.get("id") or "",
            matched_algo,
            entry,
            size,
            raw_scene=raw_matched_scene,
        )
        _scenes_set(filename, stashdb=stashdb_fragment)
        job["stashdb_contribute"] = contrib_ui
        _append_hasher_log(job_id, "done: matched")
        print(
            f"[STASHDB] {filename!r} matched on {matched_algo} → "
            f"scene {matched_scene.get('id')}",
            flush=True,
        )
    else:
        # No match — clear any stale match info from a prior run.
        _scenes_clear_match(filename)
        job["stashdb_match"] = None
        job["stashdb_contribute"] = None
        job["stashdb_helper_url"] = f"/stashdb-scene-draft?filename={quoted}"
        _append_hasher_log(job_id, "done: no match; draft helper set")
        if not job.get("stashdb_check_error"):
            print(f"[STASHDB] {filename!r} no match (OSHASH/PHASH all clean)", flush=True)

    return _scenes_get(filename)


def _parallel_postprocess_runner(job_id: str, downloaded_path: str) -> None:
    """Sidecar thread: thumber → hasher/StashDB (sequential), parallel with upload.

    Runs in a background thread while the main worker does split (if needed) and
    upload. Thumber and hasher must not overlap — both are CPU/ffmpeg heavy.
    Optional PHASH after a StashDB match runs in a third thread so finalize does
    not wait for PHASH.
    """
    try:
        _run_thumber(job_id, downloaded_path, reset_logs=True, parallel_with_upload=True)
    except Exception as e:  # noqa: BLE001
        print(f"[THUMBER] parallel postprocess (thumber) failed: {e}", flush=True)
        _append_thumber_log(job_id, f"[thumber] parallel phase error: {e}")
    if _is_cancelled(job_id):
        return
    try:
        _stashdb_post_upload_check(job_id, downloaded_path, parallel_with_upload=True)
    except Exception as e:  # noqa: BLE001
        job = jobs.get(job_id)
        if job is not None:
            job["stashdb_check_error"] = str(e)
        print(f"[STASHDB] post-upload check failed: {e}", flush=True)
    if _is_cancelled(job_id):
        return
    job = jobs.get(job_id)
    if job and job.get("stashdb_match"):
        t = threading.Thread(
            target=_run_phash_followup_bg,
            args=(job_id, downloaded_path),
            name=f"phash-followup-{job_id}",
            daemon=True,
        )
        job["_phash_followup_thread"] = t
        t.start()


def _run_phash_followup_bg(job_id: str, downloaded_path: str) -> None:
    """Optional PHASH after a StashDB match — runs outside the upload-sidecar join."""
    job = jobs.get(job_id)
    if job:
        job["phash_followup_pending"] = True
    try:
        _stashdb_phash_followup(job_id, downloaded_path)
    except Exception as e:  # noqa: BLE001
        print(f"[STASHDB] optional PHASH follow-up failed: {e}", flush=True)
    finally:
        j = jobs.get(job_id)
        if j:
            j["phash_followup_pending"] = False
            j.pop("hash_progress", None)


def _start_parallel_upload_sidecars(job_id: str, full_path: str) -> None:
    """Background thread: thumber → hasher while the main thread uploads.

    If StashDB matches, optional PHASH for contribute runs in another thread so
    the upload worker can finalize (BBCode helper) without waiting for PHASH.
    """
    job = jobs.get(job_id)
    if not job or not full_path or not os.path.isfile(full_path):
        return
    if not _is_video_file(full_path):
        return
    job.pop("_parallel_postprocess_thread", None)
    t = threading.Thread(
        target=_parallel_postprocess_runner,
        args=(job_id, full_path),
        name=f"parallel-postprocess-{job_id}",
        daemon=True,
    )
    job["_parallel_postprocess_thread"] = t
    t.start()


def _join_parallel_upload_sidecars(job_id: str, *, timeout: float | None = None) -> None:
    """Wait for the thumbnails→StashDB postprocess thread (not optional PHASH follow-up)."""
    job = jobs.get(job_id)
    if not job:
        return
    t = job.pop("_parallel_postprocess_thread", None)
    if t is None:
        return
    if not t.is_alive():
        return
    t.join(timeout=timeout)
    if t.is_alive():
        print(
            f"[PARALLEL] job {job_id}: postprocess thread still running after join(timeout={timeout!r})",
            flush=True,
        )


def _join_phash_followup_thread(job_id: str, *, timeout: float | None = None) -> None:
    """Wait for optional PHASH follow-up (matched scenes). Call before deleting the temp download."""
    job = jobs.get(job_id)
    if not job:
        return
    t = job.pop("_phash_followup_thread", None)
    if t is None:
        return
    if not t.is_alive():
        return
    t.join(timeout=timeout)
    if t.is_alive():
        print(
            f"[PARALLEL] job {job_id}: PHASH follow-up still running after join(timeout={timeout!r})",
            flush=True,
        )


@app.route("/api/cached_files")
def api_cached_files():
    """List paths under /downloads documented in scenes.json (all files we have ever hashed)."""
    q = (request.args.get("q") or "").strip().lower()
    with _scenes_lock:
        data = _scenes_load()
    files = []
    for fn in sorted(data.keys()):
        if q and q not in fn.lower():
            continue
        entry = data.get(fn)
        if not isinstance(entry, dict):
            continue
        stash = entry.get("stashdb") if isinstance(entry.get("stashdb"), dict) else {}
        scene_id = (stash.get("scene_id") or "").strip()
        edit_id = (stash.get("edit_id") or "").strip()
        draft_id = (stash.get("draft_id") or "").strip()
        files.append({
            "filename": fn,
            "scene_id": scene_id or None,
            "edit_id": edit_id or None,
            "draft_id": draft_id or None,
            "matched_by": stash.get("matched_by"),
        })
    return jsonify({"ok": True, "files": files})


@app.route("/api/scenes_stashdb_link", methods=["POST"])
@app.route("/api/hashes_stashdb_link", methods=["POST"])
def api_scenes_stashdb_link():
    """Attach StashDB scene/edit/draft ids to a scenes.json basename (e.g. after BBCode autofill)."""
    data = request.get_json(silent=True) or {}
    try:
        name = _safe_downloads_mount_rel((data.get("filename") or "").strip())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not name:
        return jsonify({"error": "filename is required"}), 400

    scene_id = (data.get("scene_id") or "").strip() or None
    edit_id = (data.get("edit_id") or "").strip() or None
    draft_id = (data.get("draft_id") or "").strip() or None
    matched_by = (data.get("matched_by") or "").strip() or None
    if not scene_id and not edit_id and not draft_id:
        return jsonify({"error": "scene_id, edit_id, or draft_id is required"}), 400

    stash = _scenes_merge_stashdb_link(
        name,
        scene_id=scene_id,
        edit_id=edit_id,
        draft_id=draft_id,
        matched_by=matched_by,
    )
    return jsonify({"ok": True, "filename": name, "stashdb": stash})


@app.route("/api/cached_scene")
@app.route("/api/cached_hashes")
def api_cached_scene():
    """Return the cached scene entry for a basename, or {} if none.

    Used by the draft page (and any external script) so we never recompute
    hashes that we already have, and so the user can still draft for files
    that have been deleted from /downloads after a previous run.
    """
    raw = (request.args.get("filename") or "").strip()
    try:
        name = _safe_downloads_mount_rel(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    entry = _scenes_get(name) or {}
    return jsonify({"ok": True, "filename": name, "entry": entry})


@app.route("/api/scenes_bbcode_save", methods=["POST"])
def api_scenes_bbcode_save():
    """Save manual BBCode fields for a cached basename (studio, gallery, etc.)."""
    data = request.get_json(silent=True) or {}
    try:
        name = _safe_downloads_mount_rel((data.get("filename") or "").strip())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not name:
        return jsonify({"error": "filename is required"}), 400
    studio = (data.get("studio") or "").strip()
    gallery_link = (data.get("gallery_link") or "").strip()
    if not studio and not gallery_link:
        return jsonify({"error": "studio or gallery_link is required"}), 400
    bbcode = _scenes_merge_bbcode(
        name,
        studio=studio,
        gallery_link=gallery_link,
        title=(data.get("title") or "").strip(),
        upload_date=(data.get("upload_date") or "").strip(),
        resolution=(data.get("resolution") or "").strip(),
        stashdb_url=(data.get("stashdb_url") or "").strip(),
        scene_id=(data.get("scene_id") or "").strip() or None,
    )
    return jsonify({"ok": True, "filename": name, "bbcode": bbcode})


@app.route("/api/resolution_presets", methods=["GET"])
def api_resolution_presets_list():
    """Learned width×height → K labels (from BBCode copy corrections)."""
    data = _resolution_presets_load()
    presets = []
    for key, row in sorted(data.items()):
        if not isinstance(row, dict):
            continue
        label = _normalize_resolution_label(row.get("label") or "")
        w = row.get("width")
        h = row.get("height")
        if not label or w is None or h is None:
            continue
        presets.append({
            "key": key,
            "width": int(w),
            "height": int(h),
            "label": label,
            "updated_at": row.get("updated_at"),
        })
    return jsonify({"ok": True, "presets": presets})


@app.route("/api/resolution_presets/learn", methods=["POST"])
def api_resolution_presets_learn():
    """Remember a corrected resolution label for a pixel size (BBCode copy)."""
    data = request.get_json(silent=True) or {}
    try:
        w = int(data.get("width"))
        h = int(data.get("height"))
    except (TypeError, ValueError):
        return jsonify({"error": "width and height are required integers"}), 400
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "label is required (e.g. 7K)"}), 400
    try:
        row = _resolution_presets_learn(w, h, label)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "ok": True,
        "preset": row,
        "key": _resolution_preset_key(w, h),
        "display": f"{row['label']} ({w}x{h})",
    })


@app.route("/api/studio_aliases", methods=["GET"])
def api_studio_aliases_list():
    """List studio aliases (StashDB studio name → GoFile folder label). Not merged into gofile_folders."""
    data = _studio_aliases_load()
    rows = []
    for key, row in data.items():
        if not isinstance(row, dict):
            continue
        stash = (row.get("stashdb_studio") or key or "").strip()
        folder = (row.get("folder_name") or "").strip()
        display = (row.get("studio_display") or "").strip()
        if stash and folder and not display:
            display = f"{stash} / {folder}"
        rows.append({
            "key": key,
            "stashdb_studio": stash,
            "folder_name": folder,
            "studio_display": display,
            "updated_at": row.get("updated_at"),
        })
    rows.sort(key=lambda r: (r.get("stashdb_studio") or "").casefold())
    return jsonify({"ok": True, "aliases": rows})


@app.route("/api/studio_aliases", methods=["POST"])
def api_studio_aliases_save():
    data = request.get_json(silent=True) or {}
    stash = (data.get("stashdb_studio") or "").strip()
    folder = (data.get("folder_name") or "").strip()
    if not stash or not folder:
        return jsonify({"error": "stashdb_studio and folder_name are required"}), 400
    display = (data.get("studio_display") or "").strip()
    if not display:
        display = f"{stash} / {folder}"
    key = _studio_alias_key(stash)
    store = _studio_aliases_load()
    store[key] = {
        "stashdb_studio": stash,
        "folder_name": folder,
        "studio_display": display,
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _studio_aliases_save(store)
    return jsonify({"ok": True, "alias": store[key], "key": key})


@app.route("/api/studio_aliases", methods=["DELETE"])
def api_studio_aliases_delete():
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    stash = (data.get("stashdb_studio") or "").strip()
    if not key and stash:
        key = _studio_alias_key(stash)
    if not key:
        return jsonify({"error": "key or stashdb_studio is required"}), 400
    store = _studio_aliases_load()
    if key not in store:
        return jsonify({"error": "alias not found"}), 404
    store.pop(key, None)
    _studio_aliases_save(store)
    return jsonify({"ok": True})


@app.route("/api/studio_threads", methods=["GET"])
def api_studio_threads_list():
    """List optional per-studio forum thread URLs (not required for posting)."""
    data = _studio_threads_load()
    rows = []
    for key, row in data.items():
        if not isinstance(row, dict):
            continue
        stash = (row.get("stashdb_studio") or key or "").strip()
        thread_url = (row.get("thread_url") or "").strip()
        if not stash or not thread_url:
            continue
        rows.append({
            "key": key,
            "stashdb_studio": stash,
            "thread_url": thread_url,
            "updated_at": row.get("updated_at"),
        })
    rows.sort(key=lambda r: (r.get("stashdb_studio") or "").casefold())
    return jsonify({"ok": True, "threads": rows})


@app.route("/api/studio_threads", methods=["POST"])
def api_studio_threads_save():
    data = request.get_json(silent=True) or {}
    stash = (data.get("stashdb_studio") or "").strip()
    thread_url = (data.get("thread_url") or "").strip()
    if not stash or not thread_url:
        return jsonify({"error": "stashdb_studio and thread_url are required"}), 400
    key = _studio_thread_key(stash)
    store = _studio_threads_load()
    store[key] = {
        "stashdb_studio": stash,
        "thread_url": thread_url,
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _studio_threads_save(store)
    return jsonify({"ok": True, "thread": store[key], "key": key})


@app.route("/api/studio_threads", methods=["DELETE"])
def api_studio_threads_delete():
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    stash = (data.get("stashdb_studio") or "").strip()
    if not key and stash:
        key = _studio_thread_key(stash)
    if not key:
        return jsonify({"error": "key or stashdb_studio is required"}), 400
    store = _studio_threads_load()
    if key not in store:
        return jsonify({"error": "thread not found"}), 404
    store.pop(key, None)
    _studio_threads_save(store)
    return jsonify({"ok": True})


@app.route("/api/performer_links_lookup", methods=["POST"])
def api_performer_links_lookup():
    """Lookup previously saved performer links by exact performer name."""
    data = request.get_json(silent=True) or {}
    names = data.get("names")
    if not isinstance(names, list):
        names = []
    links = _performers_lookup(names)
    return jsonify({"ok": True, "links": links})


@app.route("/api/performer_links_save", methods=["POST"])
def api_performer_links_save():
    """Persist performer links learned from helper UI copy action."""
    data = request.get_json(silent=True) or {}
    rows = data.get("rows")
    if not isinstance(rows, list):
        return jsonify({"error": "rows must be an array"}), 400
    changed = _performers_upsert(rows)
    return jsonify({"ok": True, "saved": changed})


@app.route("/api/thumb_sheet")
def api_thumb_sheet():
    """Serve the PNG contact sheet thumber writes beside the video (THUMBER_OUT_DIR / /thumbs).

    Query: ``video`` or ``filename`` = path under the downloads mount (e.g. ``clip.mp4`` or ``vrp/clip.mp4``).
    Optional ``attachment=1`` to force download. For inline use (``<img src>``), omit ``attachment``."""
    raw = (request.args.get("video") or request.args.get("filename") or "").strip()
    if not raw:
        return jsonify({"error": "video path is required (?video= or ?filename=)"}), 400
    try:
        _safe_downloads_mount_rel(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    path = _thumb_sheet_path(raw)
    if not path:
        return jsonify({
            "error": "thumbnail_sheet_not_found",
            "detail": "Expected <stem>_thumbs.png under the thumber output directory. "
            "Mount the same host folder as thumber's THUMBER_THUMBS at THUMBS_DIR=/thumbs.",
        }), 404
    attach = (request.args.get("attachment") or "").strip().lower() in (
        "1", "true", "yes", "download",
    )
    return send_file(
        path,
        mimetype="image/png",
        as_attachment=attach,
        download_name=os.path.basename(path),
    )


@app.route("/api/stashdb_scene_full", methods=["POST"])
def api_stashdb_scene_full():
    """Enriched single-scene fetch for /bbcode.

    Body: {"scene_id": "<uuid>"}. Filters out MALE performers (and any other
    EXCLUDED_GENDERS). Returns the largest scene image first."""
    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set in container environment"}), 400
    data = request.get_json(silent=True) or {}
    scene_id = (data.get("scene_id") or "").strip()
    if not scene_id:
        return jsonify({"error": "scene_id is required"}), 400
    try:
        raw = _stashdb_scene_full_query(scene_id)
    except Exception as e:  # noqa: BLE001
        detail = str(e)
        if isinstance(e, requests.HTTPError) and e.response is not None:
            body = (e.response.text or "").strip()
            if body:
                detail = f"{detail} | body: {body[:600]}"
        return jsonify({"error": f"Failed to fetch from StashDB: {detail}"}), 502
    if not raw:
        return jsonify({"error": "Scene not found on StashDB"}), 404
    scene = _scene_fragment_to_full(raw)
    return jsonify({"ok": True, "scene": scene})


@app.route("/api/stashdb_scene_image_png")
def api_stashdb_scene_image_png():
    """Fetch a StashDB scene image and re-encode as PNG.

    Query: ?scene_id=<id>&index=<n>  (index defaults to 0; the helper page
    can pass index=1 to grab a backup image). The user explicitly wanted
    PNG output because saving WebP from the StashDB website was annoying."""
    if not STASHDB_API_KEY:
        return jsonify({"error": "STASHDB_API_KEY is not set in container environment"}), 400
    scene_id = (request.args.get("scene_id") or "").strip()
    if not scene_id:
        return jsonify({"error": "scene_id is required"}), 400
    try:
        index = int(request.args.get("index", "0"))
    except ValueError:
        return jsonify({"error": "index must be an integer"}), 400

    try:
        png_bytes = _stashdb_scene_cover_png_bytes(scene_id, index)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    out = BytesIO(png_bytes)
    out.seek(0)
    return send_file(
        out,
        mimetype="image/png",
        as_attachment=True,
        download_name=f"{scene_id}_{index}.png",
    )


def _stashdb_scene_images_sorted(scene_id: str) -> list[dict]:
    raw = _stashdb_scene_full_query(scene_id)
    if not raw:
        raise ValueError("Scene not found on StashDB")
    images = []
    for img in raw.get("images") or []:
        if isinstance(img, dict) and img.get("url"):
            images.append(img)
    images.sort(key=lambda i: (i.get("width") or 0) * (i.get("height") or 0), reverse=True)
    if not images:
        raise ValueError("Scene has no images on StashDB")
    return images


def _stashdb_scene_cover_png_bytes(scene_id: str, index: int = 0) -> bytes:
    images = _stashdb_scene_images_sorted(scene_id)
    if index < 0 or index >= len(images):
        raise ValueError(f"index out of range (0..{len(images)-1})")
    src_url = images[index]["url"]
    try:
        r = requests.get(src_url, timeout=30, stream=False)
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to fetch image from StashDB: {e}") from e

    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Pillow is not installed; rebuild the apu image") from e

    try:
        img = Image.open(BytesIO(r.content))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            out_img = img.convert("RGBA")
        else:
            out_img = img.convert("RGB")
        out = BytesIO()
        out_img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        raise RuntimeError(f"Failed to convert image: {e}") from e


def _thumb_sheet_png_bytes(video_key: str) -> bytes | None:
    path = _thumb_sheet_path(video_key)
    if not path:
        return None
    with open(path, "rb") as fp:
        return fp.read()


def _which_ffprobe() -> str | None:
    path = shutil.which("ffprobe")
    return path if path else None


def _ffprobe_video_size(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream, or (0, 0) on failure."""
    ffprobe = _which_ffprobe()
    if not ffprobe or not os.path.isfile(path):
        return 0, 0
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            return 0, 0
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return 0, 0
        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
        return w, h
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, 0


def _ffprobe_gif_animated_pixels(path: str) -> tuple[int, int, int, int]:
    """Return (width, height, frame_count, width*height*frames) for a GIF."""
    ffprobe = _which_ffprobe()
    if not ffprobe or not os.path.isfile(path):
        return 0, 0, 0, 0
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=width,height,nb_read_frames",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            return 0, 0, 0, 0
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return 0, 0, 0, 0
        st = streams[0]
        w = int(st.get("width") or 0)
        h = int(st.get("height") or 0)
        frames = int(st.get("nb_read_frames") or 0)
        total = w * h * frames if w > 0 and h > 0 and frames > 0 else 0
        return w, h, frames, total
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, 0, 0, 0


def _slr_gif_frame_count(fps: int, duration: int) -> int:
    return max(1, fps) * max(1, duration)


def _slr_gif_animated_pixels(width: int, height: int, fps: int, duration: int) -> int:
    return max(1, width) * max(1, height) * _slr_gif_frame_count(fps, duration)


def _clamp_slr_gif_params(
    src_w: int,
    src_h: int,
    fps: int,
    width: int,
    duration: int,
    *,
    max_pixels: int,
    max_frames: int,
) -> tuple[int, int, int, int]:
    """Fit SLR GIF encode params under host frame and animated-pixel caps (0 = no cap)."""
    fps = max(1, min(fps, 60))
    width = max(120, min(width, 1920))
    duration = max(1, min(duration, 60))

    if src_w <= 0 or src_h <= 0:
        out_h = width
    else:
        out_h = max(1, int(round(width * src_h / src_w)))

    def total(w: int, d: int, f: int) -> int:
        h = max(1, int(round(w * src_h / src_w))) if src_w > 0 and src_h > 0 else w
        return _slr_gif_animated_pixels(w, h, f, d)

    if max_frames > 0:
        while _slr_gif_frame_count(fps, duration) > max_frames and duration > 1:
            duration -= 1
        while _slr_gif_frame_count(fps, duration) > max_frames and fps > 1:
            fps -= 1

    if max_pixels > 0:
        while total(width, duration, fps) > max_pixels and duration > 3:
            duration -= 1
        while total(width, duration, fps) > max_pixels and fps > 6:
            fps -= 1
        while total(width, duration, fps) > max_pixels and width > 240:
            width -= 40

        if total(width, duration, fps) > max_pixels and src_w > 0 and src_h > 0:
            frames = _slr_gif_frame_count(fps, duration)
            aspect = src_h / src_w
            w_cap = int((max_pixels / (aspect * frames)) ** 0.5)
            width = max(120, min(width, w_cap))

    out_h = max(1, int(round(width * src_h / src_w))) if src_w > 0 and src_h > 0 else width
    return fps, width, duration, out_h


def _slr_preview_gif_bytes(code: str) -> bytes:
    if not re.fullmatch(r"\d{4,10}", code):
        raise ValueError("Missing or invalid SLR scene code (4-10 digits)")
    ffmpeg_bin = _which_ffmpeg()
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg is not installed on the server")

    mp4_url = _slr_preview_mp4_url(code)
    max_mp4 = _env_int("SLR_PREVIEW_MAX_MP4_BYTES", 30_000_000)
    try:
        with requests.get(mp4_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            buf = BytesIO()
            n = 0
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                n += len(chunk)
                if n > max_mp4:
                    raise ValueError(f"Preview MP4 exceeds {max_mp4} bytes cap")
                buf.write(chunk)
            mp4_data = buf.getvalue()
    except requests.RequestException as e:
        raise RuntimeError(f"Could not download SLR preview: {e}") from e

    if len(mp4_data) < 256:
        raise RuntimeError("Preview download was empty or too small")

    limits = GIF_ENCODE_LIMITS
    fps = max(1, min(limits.fps, 60))
    width = max(120, min(limits.width, 1920))
    duration = max(1, min(limits.max_duration, 60))

    tmp_mp4 = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_gif = tempfile.NamedTemporaryFile(suffix=".gif", delete=False)
    mp4_path = tmp_mp4.name
    gif_path = tmp_gif.name
    tmp_mp4.close()
    tmp_gif.close()
    gif_bytes = b""
    gw = gh = gframes = gtotal = 0
    try:
        with open(mp4_path, "wb") as f:
            f.write(mp4_data)

        src_w, src_h = _ffprobe_video_size(mp4_path)
        req_fps, req_w, req_d = fps, width, duration
        fps, width, duration, out_h = _clamp_slr_gif_params(
            src_w,
            src_h,
            fps,
            width,
            duration,
            max_pixels=limits.max_animated_pixels,
            max_frames=limits.max_frames,
        )
        est_pixels = _slr_gif_animated_pixels(width, out_h, fps, duration)
        est_frames = _slr_gif_frame_count(fps, duration)
        if (fps, width, duration) != (req_fps, req_w, req_d):
            cap_bits = []
            if limits.max_frames > 0:
                cap_bits.append(f"≤{limits.max_frames} frames")
            if limits.max_animated_pixels > 0:
                cap_bits.append(f"≤{limits.max_animated_pixels:,} px")
            cap_desc = ", ".join(cap_bits) if cap_bits else "host limits"
            print(
                f"[SLR-GIF] Clamped for {limits.label} ({cap_desc}): "
                f"{req_w}px@{req_fps}fps×{req_d}s → {width}px@{fps}fps×{duration}s "
                f"(~{est_frames} frames, ~{est_pixels:,} px, {out_h}px tall)",
                flush=True,
            )

        vf = limits.ffmpeg_vf(fps, width)
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                mp4_path,
                "-t",
                str(duration),
                "-vf",
                vf,
                "-loop",
                "0",
                gif_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
            raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {tail or 'no stderr'}")
        with open(gif_path, "rb") as gf:
            gif_bytes = gf.read()
        gw, gh, gframes, gtotal = _ffprobe_gif_animated_pixels(gif_path)
    finally:
        for pth in (mp4_path, gif_path):
            try:
                os.unlink(pth)
            except OSError:
                pass

    if not gif_bytes or len(gif_bytes) < 32:
        raise RuntimeError("ffmpeg produced an empty GIF")
    if limits.max_bytes > 0 and len(gif_bytes) > limits.max_bytes:
        raise RuntimeError(
            f"GIF is {len(gif_bytes):,} bytes (limit {limits.max_bytes:,}). "
            f"Lower {limits.env_prefix}_GIF_WIDTH / {limits.env_prefix}_GIF_FPS / "
            f"{limits.env_prefix}_GIF_MAX_DURATION."
        )

    if limits.max_animated_pixels > 0 and gtotal > limits.max_animated_pixels:
        raise RuntimeError(
            f"GIF is {gtotal:,} animated pixels ({gw}×{gh}×{gframes} frames) — "
            f"{limits.label} limit is {limits.max_animated_pixels:,} (width×height×frames). "
            f"Lower {limits.env_prefix}_GIF_WIDTH / {limits.env_prefix}_GIF_FPS / "
            f"{limits.env_prefix}_GIF_MAX_DURATION."
        )
    if limits.max_frames > 0 and gframes > limits.max_frames:
        raise RuntimeError(
            f"GIF has {gframes} frames — {limits.label} limit is {limits.max_frames}. "
            f"Lower {limits.env_prefix}_GIF_FPS / {limits.env_prefix}_GIF_MAX_DURATION."
        )
    if gtotal > 0:
        print(
            f"[SLR-GIF] {gw}×{gh}, {gframes} frames, {len(gif_bytes):,} bytes, "
            f"{gtotal:,} animated px",
            flush=True,
        )
    return gif_bytes


def _extract_slr_scene_code(url: str) -> str | None:
    """Numeric SLR scene id from a SexLikeReal /scenes/... URL (e.g. …-78976 → 78976)."""
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        p = urllib.parse.urlparse(raw)
    except ValueError:
        return None
    host = (p.netloc or "").lower()
    if "@" in host:
        host = host.split("@")[-1]
    if ":" in host:
        host = host.split(":")[0]
    if not host.endswith("sexlikereal.com"):
        return None
    parts = [x for x in (p.path or "").split("/") if x]
    if not parts or parts[0].lower() != "scenes":
        return None
    slug = parts[-1]
    m = re.search(r"-(\d{4,10})$", slug) or re.fullmatch(r"(\d{4,10})", slug)
    return m.group(1) if m else None


def _slr_preview_mp4_url(code: str) -> str:
    return f"https://cdn-vr.sexlikereal.com/preview/14x1/{code}_300p.mp4"


def _which_ffmpeg() -> str | None:
    path = shutil.which("ffmpeg")
    return path if path else None


@app.route("/api/pixhost_upload", methods=["POST"])
def api_pixhost_upload():
    """Proxy image(s) to PiXhost API v2 as standalone uploads; returns BBCode."""
    if GIF_HOST != "pixhost":
        return jsonify({"error": f"PiXhost upload is disabled (GIF_HOST={GIF_HOST})"}), 503
    import pixhost_upload

    raw_files = request.files.getlist("file")
    if not raw_files:
        one = request.files.get("file")
        raw_files = [one] if one and one.filename else []

    parsed: list[tuple[str, bytes, str]] = []
    for f in raw_files:
        if not f or not f.filename:
            continue
        try:
            data = f.read()
        except OSError as e:
            return jsonify({"error": f"Could not read upload: {e}"}), 400
        if not data:
            continue
        filename = os.path.basename(f.filename) or "upload.bin"
        content_type = (f.content_type or "application/octet-stream").strip()
        parsed.append((filename, data, content_type))

    if not parsed:
        return jsonify({"error": "Missing file (multipart field name: file)"}), 400

    uploads: list[dict] = []
    try:
        for filename, data, content_type in parsed:
            body = pixhost_upload.upload_bytes(data, filename, content_type=content_type)
            uploads.append(
                {
                    "bbcode": body.get("bbcode") or "",
                    "direct_url": body.get("direct_url") or "",
                    "show_url": body.get("show_url") or "",
                    "th_url": body.get("th_url") or "",
                    "manage_url": body.get("manage_url") or "",
                }
            )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    if len(uploads) == 1:
        return jsonify(uploads[0])

    return jsonify(
        {
            "bbcode": " ".join(u["bbcode"] for u in uploads if u.get("bbcode")),
            "uploads": uploads,
        }
    )


@app.route("/api/slr_preview_gif")
def api_slr_preview_gif():
    """Download SLR CDN preview MP4 and return a GIF (on-demand; used by BBCode helper).

    Query: ?code=<digits>  or  ?scene_url=<encoded SLR scene page URL>
    Only SexLikeReal /scenes/ URLs are accepted for scene_url (code is extracted).
    """
    code = (request.args.get("code") or "").strip()
    scene_url = (request.args.get("scene_url") or "").strip()
    if not code and scene_url:
        try:
            scene_url = urllib.parse.unquote(scene_url)
        except Exception:  # noqa: BLE001
            scene_url = request.args.get("scene_url") or ""
        code = _extract_slr_scene_code(scene_url) or ""
    if not re.fullmatch(r"\d{4,10}", code):
        return jsonify({"error": "Missing or invalid SLR scene code (4-10 digits)"}), 400
    try:
        gif_bytes = _slr_preview_gif_bytes(code)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    out_gif = BytesIO(gif_bytes)
    out_gif.seek(0)
    return send_file(
        out_gif,
        mimetype="image/gif",
        as_attachment=True,
        download_name=f"slr_preview_{code}.gif",
    )


@app.route("/api/browse")
def api_browse():
    path = request.args.get("path", DOWNLOADS_DIR)
    if not path.startswith(DOWNLOADS_DIR):
        return jsonify({"error": "Access denied"}), 403
    try:
        entries = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            is_dir = os.path.isdir(full)
            size = 0
            if not is_dir:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    pass
            entries.append({"name": name, "path": full, "is_dir": is_dir, "size": size})
        return jsonify({"entries": entries})
    except FileNotFoundError:
        return jsonify({"entries": []})


@app.route("/api/jobs")
def api_jobs():
    safe = {}
    for k, v in jobs.items():
        safe[k] = {key: val for key, val in v.items() if not key.startswith("_")}
    return jsonify(safe)


@app.route("/api/upload_watchdog/check")
def api_upload_watchdog_check():
    """Upload-speed watchdog check (polled by apu-watchdog sidecar). dry_run=1 skips resume prep."""
    dry_run = request.args.get("dry_run", "").strip().lower() in ("1", "true", "yes")
    result = run_watchdog_once(jobs, HASHES_DIR, dry_run=dry_run)
    status = 200
    if result.get("action") == "status_unreachable":
        status = 503
    return jsonify(result), status


@app.route("/api/job/<job_id>")
def api_job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    safe = {k: v for k, v in job.items() if not k.startswith("_")}
    return jsonify(safe)


# ── GoFile folder management ────────────────────────────────────────

@app.route("/api/upload_config")
def api_upload_config():
    return jsonify({
        "provider": UPLOAD_PROVIDER,
        "label": PROVIDER_LABEL,
        "gofile_enabled": GOFILE_ENABLED,
        "filester_enabled": FILESTER_ENABLED,
        "active_providers": ACTIVE_PROVIDERS,
        "filester_split_mode": FILESTER_SPLIT_MODE,
        "filester_split_fallback": FILESTER_SPLIT_FALLBACK,
        "filester_folder_sync_enabled": FILESTER_FOLDER_SYNC_ENABLED,
        "filester_folder_sync_interval_sec": FILESTER_FOLDER_SYNC_INTERVAL_SEC,
        "filester_folder_sync": dict(_filester_sync_state),
        "server_scan": server_scan.get_scan_status(),
        "job_links_filename_only": JOB_LINKS_FILENAME_ONLY,
        "upload_watchdog": watchdog_settings(),
    })


@app.route("/api/gofile_folders")
def api_gofile_folders():
    folders = _load_gofile_folders()
    sorted_folders = sorted(
        folders.items(),
        key=lambda item: str(item[1]).casefold(),
    )
    result = [{"id": fid, "name": name} for fid, name in sorted_folders]
    root_id = _gofile_root_id_optional()
    payload: dict = {"folders": result, "root_id": root_id}
    if not result:
        payload["warning"] = (
            f"No folders in {GOFILE_FOLDERS_FILE}. "
            "Add folders via the UI or run scripts/scrape_folders.py."
        )
    elif root_id is None and GOFILE_ENABLED:
        payload["warning"] = (
            "Loaded saved folders, but GoFile root lookup failed "
            "(check GOFILE_API_KEY). Create-folder may not work until fixed."
        )
    return jsonify(payload)


@app.route("/api/filester_folders")
def api_filester_folders():
    """Read-only Filester studio folder list."""
    folders = _load_filester_folders()
    sorted_folders = sorted(folders.items(), key=lambda item: item[1].casefold())
    result = [{"id": fid, "name": name} for fid, name in sorted_folders]
    return jsonify({"folders": result})


@app.route("/api/filester_sync_folders", methods=["POST"])
def api_filester_sync_folders():
    """Refresh cache/filester-folders.json from GET /api/v1/folders."""
    if not FILESTER_ENABLED:
        return jsonify({"error": "Filester uploads are not enabled"}), 400
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "replace").strip().lower()
    if mode not in ("replace", "merge"):
        return jsonify({"error": "mode must be replace or merge"}), 400
    try:
        if not _filester_sync_lock.acquire(blocking=False):
            return jsonify({"error": "Filester folder sync already in progress"}), 409
        try:
            summary = _sync_filester_folders_from_api(mode=mode)
            _filester_sync_state["last_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            _filester_sync_state["last_count"] = int(summary.get("count") or 0)
            _filester_sync_state["last_error"] = None
        finally:
            _filester_sync_lock.release()
        folders = _load_filester_folders()
        sorted_folders = sorted(folders.items(), key=lambda item: item[1].casefold())
        result = [{"id": fid, "name": name} for fid, name in sorted_folders]
        return jsonify({"ok": True, **summary, "folders": result})
    except Exception as e:
        print(f"[API] Filester folder sync failed: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/folder_parity")
def api_folder_parity():
    """Report GoFile/Filester folder map mismatches by display name."""
    if not (GOFILE_ENABLED and FILESTER_ENABLED):
        return jsonify({"ok": True, "skipped": True, "reason": "dual upload not enabled"})
    report = _folder_parity_report()
    report["ok"] = not report["only_gofile"] and not report["only_filester"]
    return jsonify(report)


@app.route("/api/gofile_create_folder", methods=["POST"])
def api_gofile_create_folder():
    data = request.get_json()
    parent_id = data.get("parent_id", "").strip()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Folder name required"}), 400
    try:
        if not parent_id:
            parent_id = _ensure_root()
        new_id = create_folder(parent_id, name)
        folders = _load_gofile_folders()
        folders[new_id] = name
        _save_folders(folders)
        print(f"[GOFILE] Created folder '{name}' ({new_id}) under {parent_id}", flush=True)
        return jsonify({"id": new_id, "name": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/gofile_add_folder", methods=["POST"])
def api_gofile_add_existing():
    data = request.get_json()
    raw = data.get("folder_id", "").strip()
    name = data.get("name", "").strip()
    folder_id = _parse_gofile_folder_id(raw)
    if not folder_id or not name:
        return jsonify({"error": "folder_id and name required"}), 400
    folders = _load_gofile_folders()
    folders[folder_id] = name
    _save_folders(folders)
    return jsonify({"id": folder_id, "name": name})


@app.route("/api/filester_add_folder", methods=["POST"])
def api_filester_add_existing():
    data = request.get_json()
    raw = data.get("folder_id", "").strip()
    name = data.get("name", "").strip()
    folder_id = _parse_filester_folder_id(raw)
    if not folder_id or not name:
        return jsonify({"error": "folder_id and name required"}), 400
    folders = _load_filester_folders()
    folders[folder_id] = name
    _save_filester_folders(folders)
    return jsonify({"id": folder_id, "name": name})


# ── Cancel ───────────────────────────────────────────────────────────

@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_cancel_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] in ("done", "error", "cancelled"):
        return jsonify({"error": "Job already finished"}), 400
    _cancelled.add(job_id)
    job["status"] = "cancelled"
    job["status_text"] = "Cancelled"
    job["progress"] = None
    track_remove(HASHES_DIR, job_id)
    print(f"[QUEUE] Job {job_id} cancelled", flush=True)
    return jsonify({"ok": True})


# ── Progress helpers ─────────────────────────────────────────────────

def _make_dl_progress(job_id):
    def cb(pct, downloaded, total, speed, eta):
        if _is_cancelled(job_id):
            return
        jobs[job_id]["progress"] = {
            "type": "download",
            "percent": round(pct, 1) if pct >= 0 else -1,
            "downloaded": downloaded, "total": total, "speed": speed,
            "eta": int(eta) if eta and eta >= 0 else -1,
            "downloaded_fmt": format_size(downloaded),
            "total_fmt": format_size(total) if total else "?",
            "speed_fmt": f"{format_size(speed)}/s" if speed > 0 else "starting...",
        }
        if pct >= 0 and total > 0:
            eta_str = f"ETA {int(eta)}s" if eta and eta >= 0 else ""
            jobs[job_id]["status_text"] = (
                f"Downloading: {pct:.1f}% — "
                f"{format_size(downloaded)}/{format_size(total)} "
                f"@ {format_size(speed)}/s {eta_str}"
            ).strip()
        else:
            jobs[job_id]["status_text"] = (
                f"Downloading: {format_size(downloaded)} "
                f"@ {format_size(speed)}/s" if speed > 0
                else f"Downloading: {format_size(downloaded)}..."
            )
    return cb


def _make_ul_progress(job_id, folder_name):
    return UploadProgressReporter(
        jobs,
        job_id,
        folder_name,
        format_size=format_size,
        is_cancelled=lambda: _is_cancelled(job_id),
    )


def _finalize_upload(
    job_id,
    results,
    *,
    filester_skip_reason: str | None = None,
    filester_folder_id: str | None = None,
):
    gofile_urls: list[str] = []
    filester_part_urls: list[str] = []
    split_info = None
    failed: list[str] = []

    for r in results:
        if not r.ok:
            failed.append(f"{r.provider or 'upload'}: {r.raw}")
            continue
        if r.provider == "gofile" and r.gallery_url:
            gofile_urls.append(r.gallery_url)
        elif r.provider == "filester" and r.gallery_url:
            filester_part_urls.append(r.gallery_url)
        if r.was_split and not split_info:
            split_info = {
                "part_count": r.part_count,
                "original_basename": r.original_basename,
                "split_mode": r.split_mode or FILESTER_SPLIT_MODE,
            }

    if not gofile_urls and not filester_part_urls:
        missing_url = [
            f"{r.provider}: success but no download URL in response"
            for r in results
            if r.ok is False and r.raw.get("success")
        ]
        detail = "; ".join(failed) if failed else (
            "; ".join(missing_url) if missing_url else "no download page returned"
        )
        raise RuntimeError(f"Upload failed for all destinations: {detail}")

    if failed:
        for msg in failed:
            _append_job_log(job_id, f"Partial failure: {msg}")

    effective_filester_folder = filester_folder_id
    if split_info and filester_part_urls and filester_folder_id:
        job = jobs.get(job_id) or {}
        progressive_dest = (job.get("filester_split_dest_folder_id") or "").strip()
        if progressive_dest:
            effective_filester_folder = progressive_dest
            cover_path = job.get("stashdb_cover_path")
            if cover_path:
                try_set_split_folder_thumbnail(
                    progressive_dest,
                    cover_path,
                    on_log=lambda ln: _append_job_log(job_id, ln),
                )
        else:
            studio_folder_id = (job.get("filester_folder_id") or "").strip()
            filester_raws = [
                r.raw for r in results
                if r.provider == "filester" and r.ok and r.was_split
            ]
            match = job.get("stashdb_match") or {}
            scene_title = (match.get("title") or "").strip()
            cover_path = job.get("stashdb_cover_path")
            log_fn = lambda ln: _append_job_log(job_id, ln)
            if studio_folder_id and filester_raws:
                log_fn("[Filester] Finalizing split upload (subfolder, move parts, thumbnail)…")
                original = (
                    split_info.get("original_basename")
                    or job.get("source_filename")
                    or "upload"
                )
                stem, _ext = os.path.splitext(os.path.basename(original))
                fallback_name = stem or "upload"
                effective_filester_folder = organize_split_parts_into_folder(
                    parent_folder_id=studio_folder_id,
                    folder_name=fallback_name,
                    folder_title=scene_title or None,
                    cover_image_path=cover_path if scene_title else None,
                    upload_responses=filester_raws,
                    blacklist_label=(
                        f"split upload: {scene_title} (StashDB)"
                        if scene_title
                        else f"split upload: {fallback_name}"
                    ),
                    on_log=log_fn,
                )

    gofile_url = gofile_urls[0] if gofile_urls else ""
    if filester_part_urls:
        if effective_filester_folder:
            filester_url = folder_url(effective_filester_folder, provider="filester")
        else:
            filester_url = filester_part_urls[0]
    else:
        filester_url = ""

    jobs[job_id]["status"] = "done"
    jobs[job_id]["upload_destinations"] = list({
        r.provider for r in results if r.ok and r.provider
    })
    if gofile_url:
        jobs[job_id]["download_page_gofile"] = gofile_url
    if filester_url:
        jobs[job_id]["download_page_filester"] = filester_url
    if filester_skip_reason:
        jobs[job_id]["filester_skipped_reason"] = filester_skip_reason

    parts = [u for u in (gofile_url, filester_url) if u]
    jobs[job_id]["download_page"] = " | ".join(parts)

    was_split = bool(split_info and int(split_info.get("part_count") or 0) > 1)

    source_fn = (jobs[job_id].get("source_filename") or "").strip()
    if source_fn and (gofile_url or filester_url):
        upload_meta = {
            "recorded_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "was_split": was_split,
        }
        if gofile_url:
            upload_meta["gofile_url"] = gofile_url
        if filester_url:
            upload_meta["filester_url"] = filester_url
            if was_split:
                upload_meta["filester_folder_url"] = filester_url
            else:
                upload_meta["filester_file_url"] = filester_url
        if filester_skip_reason:
            upload_meta["filester_skipped_reason"] = filester_skip_reason
        _scenes_set(source_fn, upload=upload_meta)

    if split_info:
        jobs[job_id]["split"] = split_info
        if source_fn:
            split_entry = {
                "was_split": was_split,
                "part_count": split_info["part_count"],
                "original_basename": split_info["original_basename"],
                "split_mode": split_info.get("split_mode") or FILESTER_SPLIT_MODE,
                "recorded_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            if was_split and filester_url:
                split_entry["folder_url"] = filester_url
            _scenes_set(source_fn, split=split_entry)
    elif source_fn and (gofile_url or filester_url):
        _scenes_set(
            source_fn,
            split={
                "was_split": False,
                "part_count": 1,
                "recorded_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )

    helper = jobs[job_id].get("stashdb_helper_url") or ""
    if isinstance(helper, str) and helper.startswith("/bbcode"):
        extra: dict[str, str] = {}
        if gofile_url:
            extra["gallery"] = gofile_url
        if filester_url:
            extra["gallery_filester"] = filester_url
        if split_info and split_info.get("part_count", 0) > 1:
            extra["split_parts"] = str(split_info["part_count"])
            if split_info.get("original_basename"):
                extra["split_name"] = split_info["original_basename"]
            mode = split_info.get("split_mode") or FILESTER_SPLIT_MODE
            if mode:
                extra["split_mode"] = mode
        if extra:
            sep = "&" if "?" in helper else "?"
            jobs[job_id]["stashdb_helper_url"] = helper + sep + urllib.parse.urlencode(extra)

    status_bits = ["Complete"]
    if gofile_url and filester_url:
        status_bits.append("GoFile + Filester")
    elif gofile_url:
        status_bits.append("GoFile")
    elif filester_url:
        status_bits.append("Filester")
    if filester_skip_reason:
        status_bits.append("Filester skipped")
    jobs[job_id]["status_text"] = " — ".join(status_bits)
    jobs[job_id]["progress"] = {"type": "upload", "percent": 100}
    delete_upload_resume_state(resume_job_dir(HASHES_DIR, job_id))


def _restore_interrupted_upload_on_startup() -> int:
    """Re-enqueue a job saved by the upload watchdog before container restart."""
    record = load_interrupted_job(HASHES_DIR)
    if not record:
        return 0
    job_id = (record.get("job_id") or "").strip() or uuid.uuid4().hex[:12]
    kind = (record.get("job_kind") or "path").strip()
    folder_id = (record.get("folder_id") or "").strip() or None
    source_path = (record.get("source_path") or "").strip()
    source_url = (record.get("source_url") or "").strip()
    reason = (record.get("reason") or "upload watchdog restart").strip()
    clear_interrupted_job(HASHES_DIR)

    if source_path and os.path.isfile(source_path):
        _start_path_job(source_path, folder_id, job_id=job_id, resume_upload=True)
        _append_job_log(job_id, f"[Resume] {reason}")
        print(f"[WATCHDOG] Restored path upload job {job_id}", flush=True)
        return 1
    if kind == "link" and source_url:
        _start_link_job(source_url, folder_id, job_id=job_id, resume_upload=True)
        if source_path:
            jobs[job_id]["source_path"] = source_path
        _append_job_log(job_id, f"[Resume] {reason}")
        print(f"[WATCHDOG] Restored link upload job {job_id}", flush=True)
        return 1
    print(f"[WATCHDOG] Resume skipped — no file on disk ({source_path!r})", flush=True)
    return 0


# ── Upload endpoints ─────────────────────────────────────────────────

def _start_link_job(url, folder_id=None, *, job_id=None, resume_upload=False):
    """Create + enqueue a download-then-upload job for ``url``. Returns job_id."""
    url = (url or "").strip()
    folder_id = (folder_id or "").strip() or None
    folder_name = _folder_display_name(folder_id)
    print(f"[API] submit_link: {url!r}, folder_id={folder_id} ({folder_name})", flush=True)

    job_id = (job_id or "").strip() or uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "title": f"Link: {url[:80]}",
        "status": "queued",
        "status_text": "Queued...",
        "progress": None,
        "folder_name": folder_name,
        "source_url": url,
        "folder_id": folder_id or "",
        "job_kind": "link",
        "queued_at": datetime.now().isoformat(),
        "job_logs": [],
        "resume_upload": bool(resume_upload),
    }
    if not resume_upload:
        track_add(HASHES_DIR, {
            "job_id": job_id,
            "type": "link",
            "url": url,
            "folder_id": folder_id or "",
            "folder_name": folder_name,
        })

    def _worker():
        track_remove(HASHES_DIR, job_id)
        downloaded_path = None
        resume_dir = resume_job_dir(HASHES_DIR, job_id)
        existing_path = (jobs[job_id].get("source_path") or "").strip()
        try:
            if _is_cancelled(job_id):
                return
            if resume_upload and existing_path and os.path.isfile(existing_path):
                downloaded_path = existing_path
                jobs[job_id]["status"] = "uploading"
                jobs[job_id]["source_filename"] = os.path.basename(existing_path)
                jobs[job_id]["status_text"] = (
                    f"Resuming upload of {os.path.basename(existing_path)} → {folder_name}..."
                )
                _append_job_log(job_id, f"[Resume] Using existing file {existing_path}")
            else:
                jobs[job_id]["status"] = "downloading"
                jobs[job_id]["status_text"] = "Starting download..."
                downloaded_path = download_file(
                    url,
                    on_progress=_make_dl_progress(job_id),
                    should_cancel=lambda: _is_cancelled(job_id),
                    on_log=lambda ln: _append_job_log(job_id, ln),
                )

            if _is_cancelled(job_id):
                return

            jobs[job_id]["status"] = "uploading"
            fname = os.path.basename(downloaded_path)
            jobs[job_id]["source_filename"] = fname
            jobs[job_id]["source_path"] = downloaded_path
            jobs[job_id]["progress"] = None
            is_video = _is_video_file(downloaded_path)
            if is_video:
                jobs[job_id]["status_text"] = (
                    f"Uploading {fname} (thumbnails → hash alongside upload) → {folder_name}..."
                )
                _start_parallel_upload_sidecars(job_id, downloaded_path)
            else:
                jobs[job_id]["status_text"] = f"Uploading {fname} → {folder_name}..."
            filester_folder_id = _resolve_filester_folder_id(folder_id)
            jobs[job_id]["filester_folder_id"] = filester_folder_id or ""
            if (
                is_video
                and FILESTER_ENABLED
                and os.path.getsize(downloaded_path) > FILESTER_MAX_PART_BYTES
            ):
                jobs[job_id]["filester_split_active"] = True
            fs_get_meta, fs_set_dest = _filester_split_job_accessors(job_id)
            fs_folder_lock = lambda: _filester_split_folder_lock(job_id)
            try:
                results, filester_skip, fs_url_folder = upload_source(
                    downloaded_path,
                    folder_id=folder_id,
                    filester_folder_id=filester_folder_id,
                    on_progress=_make_ul_progress(job_id, folder_name),
                    should_cancel=lambda: _is_cancelled(job_id),
                    on_log=lambda ln: _append_job_log(job_id, ln),
                    job_id=job_id,
                    delete_source_after_upload=True,
                    resume_dir=resume_dir,
                    preserve_split_artifacts=resume_upload,
                    get_split_meta=fs_get_meta,
                    set_split_dest_folder_id=fs_set_dest,
                    folder_lock=fs_folder_lock,
                )
            finally:
                if is_video:
                    t_out = 15.0 if _is_cancelled(job_id) else None
                    _join_parallel_upload_sidecars(job_id, timeout=t_out)

            if _is_cancelled(job_id):
                return

            _finalize_upload(
                job_id,
                results,
                filester_skip_reason=filester_skip,
                filester_folder_id=fs_url_folder or filester_folder_id,
            )
            if is_video:
                _join_phash_followup_thread(job_id, timeout=None)

            # Verified upload succeeded — clean up the downloaded file
            try:
                os.remove(downloaded_path)
                print(f"[CLEANUP] Deleted {downloaded_path}", flush=True)
            except OSError as e:
                print(f"[CLEANUP] Failed to delete {downloaded_path}: {e}", flush=True)

        except Exception as e:
            if not _is_cancelled(job_id):
                q_wait = _job_queue.qsize()
                _append_job_log(job_id, f"Error: {str(e)[:700]}")
                if q_wait > 0:
                    _append_job_log(job_id, f"{q_wait} job(s) still queued after this one.")
                else:
                    _append_job_log(job_id, "Queue is empty; next job will start immediately when submitted.")
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["progress"] = None
                print(f"[ERROR] Job {job_id}: {e}", flush=True)
                cleanup_split_artifacts(
                    downloaded_path or jobs[job_id].get("source_path"),
                    job_id,
                    resume_dir=resume_dir,
                )
            if downloaded_path and os.path.exists(downloaded_path):
                if _is_video_file(downloaded_path):
                    _join_parallel_upload_sidecars(job_id, timeout=30.0)
                if not _is_cancelled(job_id):
                    _append_job_log(
                        job_id,
                        f"Keeping file on disk for retry/resume: "
                        f"{os.path.basename(downloaded_path)}",
                    )
            elif not _is_cancelled(job_id):
                _append_job_log(
                    job_id,
                    "No partial download file on disk (failed before save or already cleaned).",
                )

    _enqueue(job_id, _worker)
    return job_id


@app.route("/api/resolve_realdebrid", methods=["POST"])
def api_resolve_realdebrid():
    """Resolve a Real-Debrid panel link to the nearest CDN download URL."""
    from realdebrid import (
        RealDebridError,
        is_cdn_link,
        is_panel_link,
        needs_resolution,
        unrestrict_link,
    )

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400

    if is_cdn_link(url):
        return jsonify({"ok": True, "url": url, "already_cdn": True, "resolved": False})

    if not is_panel_link(url):
        return jsonify({"ok": False, "error": "not a Real-Debrid panel link (real-debrid.com/d/…)"}), 400

    if not needs_resolution(url):
        return jsonify({"ok": True, "url": url, "resolved": False})

    try:
        payload = unrestrict_link(url)
        download = (payload.get("download") or "").strip()
        if not download:
            return jsonify({"ok": False, "error": "Real-Debrid API returned no download URL"}), 502
        return jsonify({
            "ok": True,
            "url": download,
            "source_url": url,
            "resolved": True,
            "filename": (payload.get("filename") or "").strip(),
            "filesize": payload.get("filesize"),
        })
    except RealDebridError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/submit_link", methods=["POST"])
def api_submit_link():
    data = request.get_json()
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    job_id = _start_link_job(url, data.get("folder_id"))
    return jsonify({"job_id": job_id, "queued_at": jobs[job_id].get("queued_at")})


def _start_path_job(path, folder_id=None, *, job_id=None, resume_upload=False):
    """Create + enqueue an upload job for an existing ``path``. Returns job_id."""
    path = (path or "").strip()
    folder_id = (folder_id or "").strip() or None
    folder_name = _folder_display_name(folder_id)
    print(f"[API] upload_path: {path!r}, folder_id={folder_id} ({folder_name})", flush=True)

    job_id = (job_id or "").strip() or uuid.uuid4().hex[:12]
    display_name = os.path.basename(path)
    jobs[job_id] = {
        "title": f"File: {display_name}",
        "status": "queued",
        "status_text": "Queued...",
        "progress": None,
        "folder_name": folder_name,
        "source_path": path,
        "folder_id": folder_id or "",
        "job_kind": "path",
        "queued_at": datetime.now().isoformat(),
        "job_logs": [],
        "resume_upload": bool(resume_upload),
    }
    if not resume_upload:
        track_add(HASHES_DIR, {
            "job_id": job_id,
            "type": "path",
            "path": path,
            "folder_id": folder_id or "",
            "folder_name": folder_name,
        })

    def _worker():
        track_remove(HASHES_DIR, job_id)
        resume_dir = resume_job_dir(HASHES_DIR, job_id)
        try:
            if _is_cancelled(job_id):
                return

            jobs[job_id]["status"] = "uploading"
            jobs[job_id]["progress"] = None
            if os.path.isfile(path):
                jobs[job_id]["source_filename"] = os.path.basename(path)
            is_vid_file = os.path.isfile(path) and _is_video_file(path)
            if resume_upload:
                jobs[job_id]["status_text"] = (
                    f"Resuming upload of {os.path.basename(path)} → {folder_name}..."
                )
                _append_job_log(job_id, "[Resume] Continuing Filester upload from saved state")
            elif is_vid_file:
                jobs[job_id]["status_text"] = (
                    f"Uploading {os.path.basename(path)} "
                    f"(thumbnails → hash alongside upload) → {folder_name}..."
                )
                _start_parallel_upload_sidecars(job_id, path)
            else:
                jobs[job_id]["status_text"] = f"Starting upload → {folder_name}..."
            filester_folder_id = _resolve_filester_folder_id(folder_id)
            jobs[job_id]["filester_folder_id"] = filester_folder_id or ""
            if (
                is_vid_file
                and FILESTER_ENABLED
                and os.path.getsize(path) > FILESTER_MAX_PART_BYTES
            ):
                jobs[job_id]["filester_split_active"] = True
            fs_get_meta, fs_set_dest = _filester_split_job_accessors(job_id)
            fs_folder_lock = lambda: _filester_split_folder_lock(job_id)
            try:
                results, filester_skip, fs_url_folder = upload_source(
                    path,
                    folder_id=folder_id,
                    filester_folder_id=filester_folder_id,
                    on_progress=_make_ul_progress(job_id, folder_name),
                    should_cancel=lambda: _is_cancelled(job_id),
                    on_log=lambda ln: _append_job_log(job_id, ln),
                    job_id=job_id,
                    resume_dir=resume_dir,
                    preserve_split_artifacts=resume_upload,
                    get_split_meta=fs_get_meta,
                    set_split_dest_folder_id=fs_set_dest,
                    folder_lock=fs_folder_lock,
                )
            finally:
                if is_vid_file:
                    t_out = 15.0 if _is_cancelled(job_id) else None
                    _join_parallel_upload_sidecars(job_id, timeout=t_out)
            if not _is_cancelled(job_id):
                _finalize_upload(
                    job_id,
                    results,
                    filester_skip_reason=filester_skip,
                    filester_folder_id=fs_url_folder or filester_folder_id,
                )
        except Exception as e:
            if not _is_cancelled(job_id):
                q_wait = _job_queue.qsize()
                _append_job_log(job_id, f"Error: {str(e)[:700]}")
                _append_job_log(
                    job_id,
                    f"Source path was not deleted: {path}",
                )
                if q_wait > 0:
                    _append_job_log(job_id, f"{q_wait} job(s) still queued after this one.")
                else:
                    _append_job_log(job_id, "Queue is empty; next job will start immediately when submitted.")
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["progress"] = None
                print(f"[ERROR] Job {job_id}: {e}", flush=True)
                cleanup_split_artifacts(path, job_id, resume_dir=resume_dir)
            if os.path.isfile(path) and _is_video_file(path):
                _join_parallel_upload_sidecars(job_id, timeout=25.0)

    _enqueue(job_id, _worker)
    return job_id


@app.route("/api/upload_path", methods=["POST"])
def api_upload_path():
    data = request.get_json()
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    if not path.startswith(DOWNLOADS_DIR):
        return jsonify({"error": f"Path must be inside /downloads (got: {path})"}), 403
    if not os.path.exists(path):
        return jsonify({"error": f"Path not found: {path}"}), 404
    job_id = _start_path_job(path, data.get("folder_id"))
    return jsonify({"job_id": job_id, "queued_at": jobs[job_id].get("queued_at")})


# ── Queue export/import + restore (queue tools UI) ───────────────────
_restore_pending_queue = register_queue_routes(
    app,
    jobs=jobs,
    hashes_dir=HASHES_DIR,
    downloads_dir=DOWNLOADS_DIR,
    start_link_job=_start_link_job,
    start_path_job=_start_path_job,
)

_start_filester_folder_sync_background()
server_scan.start_background_server_scans()


if __name__ == "__main__":
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    folders = _load_gofile_folders()
    print(f"[apu] Loaded {len(folders)} saved GoFile folder(s) from {GOFILE_FOLDERS_FILE}", flush=True)
    print(
        f"[apu] Upload: {PROVIDER_LABEL} "
        f"(gofile={GOFILE_ENABLED}, filester={FILESTER_ENABLED})",
        flush=True,
    )
    if _env_yes("RESTORE_QUEUE", default="1"):
        try:
            restored_watchdog = _restore_interrupted_upload_on_startup()
            if restored_watchdog:
                print(f"[apu] Restored {restored_watchdog} interrupted upload job(s)", flush=True)
            restored = _restore_pending_queue()
            print(f"[apu] Restored {restored} pending job(s) from disk", flush=True)
        except Exception as e:
            print(f"[apu] Queue restore failed: {e}", flush=True)
    print("[apu] Starting on http://0.0.0.0:5000", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
