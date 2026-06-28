"""Disk-backed pending upload queue for apu/monolith.

Survives container restarts when RESTORE_QUEUE is enabled (default on).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone


_queue_lock = threading.Lock()


def queue_file_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "pending_queue.json")


def _load(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return []


def _save(path: str, items: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, path)


def track_add(cache_dir: str, record: dict) -> None:
    path = queue_file_path(cache_dir)
    with _queue_lock:
        items = _load(path)
        items.append(record)
        _save(path, items)


def track_remove(cache_dir: str, job_id: str) -> None:
    if not job_id:
        return
    path = queue_file_path(cache_dir)
    with _queue_lock:
        items = [i for i in _load(path) if i.get("job_id") != job_id]
        _save(path, items)


def load_all(cache_dir: str) -> list:
    with _queue_lock:
        return list(_load(queue_file_path(cache_dir)))


def clear(cache_dir: str) -> None:
    with _queue_lock:
        _save(queue_file_path(cache_dir), [])


def export_pending(cache_dir: str, jobs: dict) -> dict:
    """Return queued (not yet started) items plus optional active job snapshot."""
    records = load_all(cache_dir)
    pending = []
    for rec in records:
        jid = rec.get("job_id")
        job = jobs.get(jid, {})
        status = job.get("status", "queued")
        if status != "queued":
            continue
        item = {"type": rec.get("type") or rec.get("job_kind")}
        if item["type"] == "link":
            item["url"] = rec.get("url") or job.get("source_url") or ""
        elif item["type"] == "path":
            item["path"] = rec.get("path") or job.get("source_path") or ""
        else:
            continue
        item["folder_id"] = rec.get("folder_id") or job.get("folder_id") or ""
        if rec.get("folder_name") or job.get("folder_name"):
            item["folder_name"] = rec.get("folder_name") or job.get("folder_name")
        pending.append(item)

    active = None
    for jid, job in jobs.items():
        if job.get("status") in ("downloading", "uploading", "hashing"):
            active = {
                "job_id": jid,
                "status": job.get("status"),
                "title": job.get("title"),
                "folder_id": job.get("folder_id") or "",
                "folder_name": job.get("folder_name"),
                "type": job.get("job_kind"),
            }
            if job.get("source_url"):
                active["url"] = job["source_url"]
            if job.get("source_path"):
                active["path"] = job["source_path"]
            break

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "pending": pending,
        "pending_count": len(pending),
        "queue_depth": sum(1 for j in jobs.values() if j.get("status") == "queued"),
        "active": active,
    }


def format_export_txt(payload: dict) -> str:
    lines = ["# type\turl_or_path\tfolder_id"]
    for item in payload.get("pending") or []:
        kind = item.get("type") or ""
        target = item.get("url") if kind == "link" else item.get("path", "")
        folder_id = item.get("folder_id") or ""
        lines.append(f"{kind}\t{target}\t{folder_id}")
    return "\n".join(lines) + "\n"
