"""
Background server scan:

- Real-Debrid: benchmark public CDN hosts and pick the fastest one.
- GoFile: benchmark upload servers and pick the fastest one.

When a server is pinned in .env (REAL_DEBRID_PREFERRED_CDN / GOFILE_PREFERRED_SERVER),
the scan is skipped and status is "pinned".

When the scan is enabled, results are cached to disk with TTL and applied via
runtime setters so no restart is required.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from copy import deepcopy

import realdebrid
import gofile_upload


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_cache_dir(path: str) -> str:
    path = (path or "").strip() or os.path.join(APP_DIR, "cache")
    if not os.path.isabs(path):
        path = os.path.join(APP_DIR, path)
    return os.path.realpath(path)


CACHE_DIR = _resolve_cache_dir(os.environ.get("CACHE_DIR") or os.path.join(APP_DIR, "cache"))


def _env_yes(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _split_hosts(raw: str) -> list[str]:
    parts = re.split(r"[,;\s]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


_STATE_LOCK = threading.Lock()
_SCAN_LOCK = threading.Lock()
_STARTED = False

_state: dict = {
    "realdebrid": {
        "state": "disabled",
        "target": None,
        "eta_sec": None,
        "winner": None,
        "mbps": None,
        "at": None,
        "error": None,
    },
    "gofile": {
        "state": "disabled",
        "target": None,
        "eta_sec": None,
        "winner": None,
        "mbps": None,
        "at": None,
        "error": None,
    },
}


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _load_cache(name: str) -> dict | None:
    path = _cache_path(name)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_cache(name: str, payload: dict) -> None:
    path = _cache_path(name)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _apply_realdebrid_winner(winner_host: str) -> None:
    if not winner_host:
        return
    realdebrid.set_runtime_preferred_cdn(winner_host)


def _apply_gofile_winner(winner_server_label: str) -> None:
    if not winner_server_label:
        return
    gofile_upload.set_runtime_preferred_server(winner_server_label)


def _set_provider_state(provider: str, **updates) -> None:
    with _STATE_LOCK:
        cur = _state.get(provider) or {}
        cur.update(updates)
        _state[provider] = cur


def get_scan_status() -> dict:
    with _STATE_LOCK:
        return deepcopy(_state)


def _eligible_hosts_for_scan() -> list[str]:
    raw_hosts = (os.environ.get("REAL_DEBRID_SCAN_HOSTS") or "").strip()
    if raw_hosts:
        return [realdebrid.normalize_cdn_host(h) for h in _split_hosts(raw_hosts)]
    return list(realdebrid.DEFAULT_HOSTS)


def _rd_cache_ttl_sec() -> float:
    return _env_float("REAL_DEBRID_SCAN_CACHE_TTL_SEC", default=86400.0)


def _gofile_cache_ttl_sec() -> float:
    return _env_float("GOFILE_SCAN_CACHE_TTL_SEC", default=86400.0)


def _start_realdebrid_scan_thread() -> None:
    def run() -> None:
        provider = "realdebrid"
        try:
            hosts = [h for h in _eligible_hosts_for_scan() if h]
            if not hosts:
                raise RuntimeError("No Real-Debrid CDN hosts to scan")

            _set_provider_state(
                provider,
                state="scanning",
                target={"type": "benchmark_cdn_hosts", "count": len(hosts)},
            )

            seconds = _env_float("REAL_DEBRID_SCAN_SECONDS", default=6.0)
            workers = max(1, _env_int("REAL_DEBRID_SCAN_WORKERS", default=8))
            connect_to = _env_float("REAL_DEBRID_SCAN_CONNECT_TIMEOUT_SEC", default=8.0)

            results = realdebrid.benchmark_hosts(
                hosts,
                seconds=seconds,
                workers=workers,
                connect_to=connect_to,
            )
            ok = [r for r in results if r.get("ok")]
            if not ok:
                raise RuntimeError("Real-Debrid CDN scan returned no successful results")

            ok.sort(key=lambda r: r["mbps"], reverse=True)
            top = max(1, _env_int("REAL_DEBRID_SCAN_TOP", default=20))
            winner = ok[:top][0]
            _apply_realdebrid_winner(winner["host"])

            cache_payload = {
                "host": winner["host"],
                "mbps": winner.get("mbps"),
                "at": time.time(),
            }
            _write_cache("rd-best-cdn.json", cache_payload)

            _set_provider_state(
                provider,
                state="done",
                target=None,
                winner=winner["host"],
                mbps=winner.get("mbps"),
                at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cache_payload["at"])),
                error=None,
            )
        except Exception as e:  # noqa: BLE001
            _set_provider_state(provider, state="error", error=str(e))

    threading.Thread(target=run, daemon=True, name="server-scan-realdebrid").start()


def _start_gofile_scan_thread() -> None:
    def run() -> None:
        provider = "gofile"
        try:
            include_regional = _env_yes(
                "GOFILE_SCAN_INCLUDE_REGIONAL",
                default=False,
            )
            probe_megabytes = _env_float("GOFILE_SCAN_MEGABYTES", default=3.0)
            workers = max(1, _env_int("GOFILE_SCAN_WORKERS", default=3))
            connect_to = _env_float("GOFILE_SCAN_CONNECT_TIMEOUT_SEC", default=10.0)
            read_to = _env_float("GOFILE_SCAN_READ_TIMEOUT_SEC", default=120.0)

            servers = gofile_upload.get_ordered_upload_servers(include_regional=include_regional)
            if not servers:
                raise RuntimeError("No GoFile upload servers available")

            _set_provider_state(
                provider,
                state="scanning",
                target={"type": "benchmark_upload_servers", "count": len(servers)},
            )

            probe_bytes = max(256 * 1024, int(probe_megabytes * 1024 * 1024))
            payload = os.urandom(probe_bytes)

            results = gofile_upload.benchmark_servers(
                servers,
                payload=payload,
                connect_to=connect_to,
                read_to=read_to,
                workers=workers,
            )

            ok = [r for r in results if r.get("ok")]
            if not ok:
                raise RuntimeError("GoFile server scan returned no successful results")
            ok.sort(key=lambda r: r["mbps"], reverse=True)
            top = max(1, _env_int("GOFILE_SCAN_TOP", default=15))
            winner = ok[:top][0]
            _apply_gofile_winner(winner["server"])

            cache_payload = {
                "server": winner["server"],
                "mbps": winner.get("mbps"),
                "at": time.time(),
            }
            _write_cache("gofile-best-server.json", cache_payload)

            _set_provider_state(
                provider,
                state="done",
                target=None,
                winner=winner["server"],
                mbps=winner.get("mbps"),
                at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cache_payload["at"])),
                error=None,
            )
        except Exception as e:  # noqa: BLE001
            _set_provider_state(provider, state="error", error=str(e))

    threading.Thread(target=run, daemon=True, name="server-scan-gofile").start()


def _maybe_apply_realdebrid_from_cache() -> bool:
    provider = "realdebrid"
    cached = _load_cache("rd-best-cdn.json")
    if not cached:
        return False
    ttl = _rd_cache_ttl_sec()
    if ttl <= 0:
        return False
    at = cached.get("at")
    if not isinstance(at, (int, float)):
        return False
    if time.time() - float(at) > ttl:
        return False

    host = cached.get("host")
    if not host:
        return False
    _apply_realdebrid_winner(host)
    _set_provider_state(
        provider,
        state="cached",
        target=None,
        winner=host,
        mbps=cached.get("mbps"),
        at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(at))),
        error=None,
    )
    return True


def _maybe_apply_gofile_from_cache() -> bool:
    provider = "gofile"
    cached = _load_cache("gofile-best-server.json")
    if not cached:
        return False
    ttl = _gofile_cache_ttl_sec()
    if ttl <= 0:
        return False
    at = cached.get("at")
    if not isinstance(at, (int, float)):
        return False
    if time.time() - float(at) > ttl:
        return False

    server = cached.get("server")
    if not server:
        return False
    _apply_gofile_winner(server)
    _set_provider_state(
        provider,
        state="cached",
        target=None,
        winner=server,
        mbps=cached.get("mbps"),
        at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(at))),
        error=None,
    )
    return True


def start_background_server_scans() -> None:
    """Start background scans (once per process). Safe to call multiple times."""
    # Quick exit when both providers are pinned/disabled.
    with _SCAN_LOCK:
        global _STARTED
        if _STARTED:
            return
        _STARTED = True

        # Real-Debrid pinned?
        rd_pinned = bool((os.environ.get("REAL_DEBRID_PREFERRED_CDN") or "").strip())
        gofile_pinned = bool((os.environ.get("GOFILE_PREFERRED_SERVER") or "").strip())

        # Real-Debrid auto-scan default:
        # - if pinned in env: default OFF
        # - otherwise: default ON (unless REAL_DEBRID_AUTO_SCAN=0)
        rd_auto_default = not rd_pinned
        rd_auto = _env_yes("REAL_DEBRID_AUTO_SCAN", default=rd_auto_default)

        gofile_auto_default = not gofile_pinned
        gofile_auto = _env_yes("GOFILE_AUTO_SCAN", default=gofile_auto_default)

        # GoFile scan requires API key.
        gofile_ready = bool((getattr(gofile_upload, "GOFILE_API_KEY", "") or "").strip())

        if rd_pinned:
            winner = (os.environ.get("REAL_DEBRID_PREFERRED_CDN") or "").strip()
            _set_provider_state(
                "realdebrid",
                state="pinned",
                winner=winner,
                error=None,
            )
        else:
            if not rd_auto:
                _set_provider_state("realdebrid", state="disabled", error=None)
            else:
                # Cache fast-path
                if not _maybe_apply_realdebrid_from_cache():
                    hosts = [h for h in _eligible_hosts_for_scan() if h]
                    seconds = _env_float("REAL_DEBRID_SCAN_SECONDS", default=6.0)
                    workers = max(1, _env_int("REAL_DEBRID_SCAN_WORKERS", default=8))
                    eta_sec = float(seconds) * float(math.ceil(len(hosts) / float(workers)))
                    _set_provider_state(
                        "realdebrid",
                        state="scanning",
                        target={"type": "benchmark_cdn_hosts", "count": len(hosts)},
                        eta_sec=round(eta_sec, 1),
                    )
                    _start_realdebrid_scan_thread()

        if gofile_pinned:
            winner = (os.environ.get("GOFILE_PREFERRED_SERVER") or "").strip()
            _set_provider_state(
                "gofile",
                state="pinned",
                winner=winner,
                error=None,
            )
        else:
            if not gofile_auto or not gofile_ready:
                _set_provider_state("gofile", state="disabled", error=None)
            else:
                if not _maybe_apply_gofile_from_cache():
                    # Quick ETA estimate based on probe threads.
                    include_regional = _env_yes(
                        "GOFILE_SCAN_INCLUDE_REGIONAL",
                        default=False,
                    )
                    servers = gofile_upload.get_ordered_upload_servers(include_regional=include_regional)
                    servers = servers or []
                    workers = max(1, _env_int("GOFILE_SCAN_WORKERS", default=3))
                    probe_megabytes = _env_float("GOFILE_SCAN_MEGABYTES", default=3.0)
                    # Rough ETA: small probe uploads typically complete quickly; use a conservative constant.
                    probe_hint_sec = 6.0
                    eta_sec = probe_hint_sec * float(math.ceil(len(servers) / float(workers)))
                    _set_provider_state(
                        "gofile",
                        state="scanning",
                        target={"type": "benchmark_upload_servers", "count": len(servers)},
                        eta_sec=round(eta_sec, 1),
                    )
                    _start_gofile_scan_thread()

