#!/usr/bin/env python3
"""Benchmark GoFile upload servers from the shell (no browser).

Uploads a small in-memory probe to each server and ranks throughput.
Use the winner in .env as GOFILE_PREFERRED_SERVER.

  python scripts/gofile_upload_speedtest.py
  python scripts/gofile_upload_speedtest.py --include-regional
  python scripts/gofile_upload_speedtest.py --servers store1,upload-na-phx
  python scripts/gofile_upload_speedtest.py --megabytes 5 --workers 4

Requires GOFILE_API_KEY in .env (or environment). Each probe creates a tiny guest
upload on GoFile (no folderId) — delete from your account if you care.

From Docker:

  docker compose exec apu python scripts/gofile_upload_speedtest.py --include-regional
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from requests_toolbelt import MultipartEncoder

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(APP_DIR, "py")
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)


def _load_dotenv() -> None:
    env_path = os.path.join(APP_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _mbps(bytes_sent: int, elapsed: float) -> float:
    if elapsed <= 0 or bytes_sent <= 0:
        return 0.0
    return (bytes_sent * 8) / elapsed / 1_000_000


def _probe_server(server: str, payload: bytes, connect_to: float, read_to: float) -> dict:
    import gofile_upload

    host = gofile_upload.upload_host(server)
    url = gofile_upload.upload_url(server)
    headers = gofile_upload._auth_headers()
    fields = {
        "file": ("apu_speedtest.bin", BytesIO(payload), "application/octet-stream"),
    }
    encoder = MultipartEncoder(fields=fields)
    headers["Content-Type"] = encoder.content_type

    started = time.monotonic()
    try:
        r = requests.post(url, data=encoder, headers=headers, timeout=(connect_to, read_to))
    except requests.RequestException as e:
        return {"host": host, "server": server, "ok": False, "error": str(e)[:140]}

    elapsed = time.monotonic() - started
    if r.status_code >= 400:
        snippet = (r.text or "")[:120]
        return {
            "host": host,
            "server": server,
            "ok": False,
            "error": f"HTTP {r.status_code}: {snippet}",
        }

    try:
        body = r.json()
    except ValueError:
        body = {}
    if body.get("status") != "ok":
        return {
            "host": host,
            "server": server,
            "ok": False,
            "error": str(body.get("status") or body)[:140],
        }

    return {
        "host": host,
        "server": server,
        "ok": True,
        "mbps": _mbps(len(payload), elapsed),
        "mib_s": len(payload) / elapsed / (1024 * 1024),
        "seconds": round(elapsed, 2),
        "bytes": len(payload),
    }


def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Benchmark GoFile upload servers.")
    parser.add_argument(
        "--servers",
        help="Comma-separated server names or hosts (default: API list + optional regional).",
    )
    parser.add_argument(
        "--include-regional",
        action="store_true",
        help="Also test upload.gofile.io and regional proxies from gofile.io/api.",
    )
    parser.add_argument("--megabytes", type=float, default=3.0, help="Probe upload size per server.")
    parser.add_argument("--workers", type=int, default=3, help="Parallel probes (keep low — real uploads).")
    parser.add_argument("--top", type=int, default=15, help="How many results to print.")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=120.0)
    args = parser.parse_args()

    import gofile_upload

    if not gofile_upload.GOFILE_API_KEY:
        print("GOFILE_API_KEY is not set.", file=sys.stderr)
        return 1

    if args.servers:
        server_labels = [s.strip() for s in re.split(r"[,;\s]+", args.servers) if s.strip()]
    else:
        try:
            merged = gofile_upload.get_ordered_upload_servers(include_regional=args.include_regional)
        except Exception as e:
            print(f"Failed to list servers from API: {e}", file=sys.stderr)
            return 1
        server_labels = merged

    # Dedupe by host while preserving first label
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for label, host in zip(server_labels, servers):
        h = gofile_upload.upload_host(label)
        if h in seen:
            continue
        seen.add(h)
        pairs.append((label, h))

    if not pairs:
        print("No servers to test.", file=sys.stderr)
        return 1

    probe_bytes = max(256 * 1024, int(args.megabytes * 1024 * 1024))
    payload = os.urandom(probe_bytes)
    mib = probe_bytes / (1024 * 1024)

    print(
        f"Uploading {mib:.1f} MiB probe to {len(pairs)} server(s) "
        f"({args.workers} workers)…\n"
    )

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {
            pool.submit(
                _probe_server,
                label,
                payload,
                args.connect_timeout,
                args.read_timeout,
            ): label
            for label, _host in pairs
        }
        for fut in as_completed(futs):
            results.append(fut.result())

    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    ok.sort(key=lambda r: r["mbps"], reverse=True)

    print(f"{'SERVER':<36} {'MiB/s':>8} {'Mbps':>8}  NOTE")
    print("-" * 68)
    for r in ok[: max(1, args.top)]:
        print(
            f"{r['host']:<36} {r['mib_s']:8.2f} {r['mbps']:8.1f}  "
            f"{mib:.1f} MiB in {r['seconds']}s"
        )

    if fail:
        print(f"\nFailed ({len(fail)}):")
        for r in sorted(fail, key=lambda x: x["host"])[:12]:
            print(f"  {r['host']}: {r.get('error', '?')}")
        if len(fail) > 12:
            print(f"  … and {len(fail) - 12} more")

    if ok:
        best = ok[0]
        # Prefer short API name for .env when host is storeN.gofile.io style
        suggest = best["server"]
        if suggest.endswith(".gofile.io"):
            suggest = suggest.removesuffix(".gofile.io")
        print(f"\nSuggested .env (fastest upload from this VPS):")
        print(f"GOFILE_PREFERRED_SERVER={suggest}")
        print("\nRestart apu after changing .env.")
    else:
        print("\nNo server accepted the probe upload.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
