"""Sidecar: poll apu upload watchdog and restart the apu container via Docker API.

Stdlib only — talks to apu over HTTP and to Docker via /var/run/docker.sock.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _docker_request(method: str, path: str, *, sock_path: str = "/var/run/docker.sock") -> int:
    conn = http.client.HTTPConnection("localhost")
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.connect(sock_path)
    conn.request(method, path)
    resp = conn.getresponse()
    resp.read()
    status = resp.status
    conn.close()
    return status


def restart_container(name: str) -> None:
    status = _docker_request("POST", f"/containers/{name}/restart")
    if status not in (204, 304):
        raise RuntimeError(f"docker restart {name!r} returned HTTP {status}")


def poll_watchdog(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/upload_watchdog/check"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.load(resp)
    return data if isinstance(data, dict) else {}


def main() -> None:
    if not _env_bool("UPLOAD_WATCHDOG_ENABLED", False):
        print("[apu-watchdog] UPLOAD_WATCHDOG_ENABLED is false; sleeping", flush=True)
        while True:
            time.sleep(3600)

    base_url = (os.environ.get("APU_WATCHDOG_URL") or "http://apu:5000").strip()
    container = (os.environ.get("APU_CONTAINER_NAME") or "apu").strip()
    poll_sec = max(5, _env_int("UPLOAD_WATCHDOG_POLL_SEC", 10))
    wait_after = max(10, _env_int("APU_WATCHDOG_RESTART_WAIT_SEC", 45))

    print(
        f"[apu-watchdog] polling {base_url}/api/upload_watchdog/check "
        f"every {poll_sec}s; restart target={container}",
        flush=True,
    )

    while True:
        try:
            result = poll_watchdog(base_url)
            action = str(result.get("action") or "")
            if result.get("restart_required"):
                reason = (result.get("reason") or "slow upload").strip()
                job_id = (result.get("job_id") or "").strip()
                print(
                    f"[apu-watchdog] restart required for job {job_id or '?'}: {reason}",
                    flush=True,
                )
                restart_container(container)
                print(
                    f"[apu-watchdog] restarted {container}; waiting {wait_after}s",
                    flush=True,
                )
                time.sleep(wait_after)
            elif action == "disabled":
                print("[apu-watchdog] apu reports watchdog disabled", flush=True)
        except urllib.error.URLError as exc:
            print(f"[apu-watchdog] apu unreachable: {exc}", flush=True)
        except Exception as exc:
            print(f"[apu-watchdog] error: {exc}", flush=True)

        time.sleep(poll_sec)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
