"""Upload speed watchdog — restart apu when Filester uploads stay too slow.

Only reacts during the ``uploading`` phase (not ``splitting`` or ``downloading``).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from upload_resume import resume_job_dir, save_interrupted_job

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_RESTART = 2


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def watchdog_settings() -> dict[str, Any]:
    return {
        "enabled": _env_bool("UPLOAD_WATCHDOG_ENABLED", False),
        "min_mbps": max(0.1, _env_float("UPLOAD_WATCHDOG_MIN_MBPS", 5.0)),
        "sustain_sec": max(10, _env_int("UPLOAD_WATCHDOG_SUSTAIN_SEC", 60)),
        "poll_sec": max(5, _env_int("UPLOAD_WATCHDOG_POLL_SEC", 10)),
        "cooldown_sec": max(60, _env_int("UPLOAD_WATCHDOG_COOLDOWN_SEC", 300)),
    }


def _state_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "upload-watchdog.json")


def _load_state(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(path: str, state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _format_speed(bps: float | None) -> str:
    if not bps or bps <= 0:
        return "0 B/s"
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    value = float(bps)
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    return f"{value:.1f} {units[unit]}"


def find_active_upload_job(jobs: dict) -> tuple[str | None, dict | None]:
    """Return the job currently downloading or uploading (first match)."""
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if job.get("status") in ("downloading", "uploading"):
            return job_id, job
    return None, None


def upload_phase_from_job(job: dict | None) -> str:
    if not job:
        return "idle"
    status = str(job.get("status") or "")
    if status == "downloading":
        return "downloading"
    progress = job.get("progress") or {}
    if status == "uploading" and isinstance(progress, dict):
        phase = str(progress.get("phase") or "uploading")
        return phase
    if status == "uploading":
        return "uploading"
    return "idle"


def upload_speed_bps_from_job(job: dict | None) -> float | None:
    if not job:
        return None
    progress = job.get("progress") or {}
    if not isinstance(progress, dict):
        return None
    speed = progress.get("speed")
    if speed is None:
        return None
    try:
        return float(speed)
    except (TypeError, ValueError):
        return None


def prepare_upload_restart(
    cache_dir: str,
    jobs: dict,
    *,
    job_id: str,
    reason: str,
) -> dict[str, Any]:
    """Persist interrupted job metadata so apu can resume after container restart."""
    job = jobs.get(job_id) or {}
    source_path = (job.get("source_path") or "").strip()
    if not source_path and job.get("source_filename"):
        source_path = os.path.join(
            os.environ.get("MEDIA_DOWNLOADS_DIR", "./downloads").rstrip("/") or "./downloads",
            job["source_filename"],
        )
    record = {
        "job_id": job_id,
        "job_kind": job.get("job_kind") or "path",
        "source_path": source_path,
        "source_url": job.get("source_url") or "",
        "folder_id": job.get("folder_id") or "",
        "folder_name": job.get("folder_name") or "",
        "source_filename": job.get("source_filename") or "",
        "filester_folder_id": job.get("filester_folder_id") or "",
        "resume_dir": resume_job_dir(cache_dir, job_id),
        "reason": reason,
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_interrupted_job(cache_dir, record)
    return record


def run_watchdog_once(
    jobs: dict,
    cache_dir: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    settings = watchdog_settings()
    now = time.time()
    state_path = _state_path(cache_dir)
    state = _load_state(state_path)
    min_bps = settings["min_mbps"] * 1024 * 1024

    result: dict[str, Any] = {
        "enabled": settings["enabled"],
        "min_mbps": settings["min_mbps"],
        "min_bps": min_bps,
        "sustain_sec": settings["sustain_sec"],
        "action": "none",
        "restart_required": False,
    }

    if not settings["enabled"]:
        result["action"] = "disabled"
        return result

    last_restart = float(state.get("last_restart_at") or 0)
    if last_restart and (now - last_restart) < settings["cooldown_sec"]:
        remaining = int(settings["cooldown_sec"] - (now - last_restart))
        result["action"] = "cooldown"
        result["cooldown_remaining_sec"] = remaining
        _save_state(state_path, state)
        return result

    job_id, job = find_active_upload_job(jobs)
    phase = upload_phase_from_job(job)
    speed_bps = upload_speed_bps_from_job(job)

    result.update({
        "phase": phase,
        "job_id": job_id,
        "speed_bps": speed_bps,
        "speed": _format_speed(speed_bps),
    })

    if phase != "uploading":
        state["low_since"] = None
        state["last_job_id"] = job_id
        _save_state(state_path, state)
        result["action"] = "idle"
        return result

    if speed_bps is None or speed_bps <= 0:
        result["action"] = "waiting_for_speed"
        _save_state(state_path, state)
        return result

    if speed_bps >= min_bps:
        state["low_since"] = None
        state["last_speed_bps"] = speed_bps
        state["last_job_id"] = job_id
        _save_state(state_path, state)
        result["action"] = "ok"
        return result

    low_since = state.get("low_since")
    if not low_since or state.get("last_job_id") != job_id:
        state["low_since"] = now
        state["last_job_id"] = job_id
        state["last_speed_bps"] = speed_bps
        _save_state(state_path, state)
        result["action"] = "slow_started"
        result["low_duration_sec"] = 0
        return result

    low_duration = now - float(low_since)
    result["low_duration_sec"] = round(low_duration, 1)

    if low_duration < settings["sustain_sec"]:
        state["last_speed_bps"] = speed_bps
        _save_state(state_path, state)
        result["action"] = "slow_continuing"
        return result

    reason = (
        f"Upload below {settings['min_mbps']:g} MB/s for "
        f"{int(low_duration)}s ({_format_speed(speed_bps)})"
    )
    result["action"] = "restart"
    result["restart_required"] = True
    result["reason"] = reason

    if dry_run:
        result["dry_run"] = True
        return result

    if job_id:
        record = prepare_upload_restart(cache_dir, jobs, job_id=job_id, reason=reason)
        result["interrupted_job"] = record
        print(f"[WATCHDOG] Prepared resume for job {job_id}: {reason}", flush=True)
    else:
        print(f"[WATCHDOG] Slow upload with no active job id: {reason}", flush=True)

    state["low_since"] = None
    state["last_restart_at"] = now
    state["last_speed_bps"] = speed_bps
    _save_state(state_path, state)
    return result


def format_speed(bps: float | None) -> str:
    return _format_speed(bps)
