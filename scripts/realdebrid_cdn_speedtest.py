#!/usr/bin/env python3
"""Benchmark Real-Debrid CDN hosts from the shell (no browser).

Downloads each host's public speedtest file for a few seconds and ranks throughput.
Use the winner in .env as REAL_DEBRID_PREFERRED_CDN.

  python scripts/realdebrid_cdn_speedtest.py
  python scripts/realdebrid_cdn_speedtest.py --hosts nyk7-4,44-4,den1-4
  python scripts/realdebrid_cdn_speedtest.py --seconds 8 --top 15

From Docker:

  docker compose exec apu python scripts/realdebrid_cdn_speedtest.py
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Numbered pools + named metros (community-maintained lists; not exhaustive).
DEFAULT_HOSTS = [
    *[f"{n}.download.real-debrid.com" for n in range(20, 24)],
    *[f"{n}.download.real-debrid.com" for n in range(30, 35)],
    *[f"{n}.download.real-debrid.com" for n in range(40, 46)],
    *[f"{n}.download.real-debrid.com" for n in range(50, 70)],
    "rbx.download.real-debrid.com",
    "den1.download.real-debrid.com",
    "sea1.download.real-debrid.com",
    "nyk1.download.real-debrid.com",
    "chi1.download.real-debrid.com",
    "lax1.download.real-debrid.com",
    "mia1.download.real-debrid.com",
    "dal1.download.real-debrid.com",
    "qro1.download.real-debrid.com",
    "sao1.download.real-debrid.com",
    "scl1.download.real-debrid.com",
    "lon1.download.real-debrid.com",
    "hkg1.download.real-debrid.com",
    "sgp1.download.real-debrid.com",
    "tyo1.download.real-debrid.com",
    "mum1.download.real-debrid.com",
    "tlv1.download.real-debrid.com",
    "jnb1.download.real-debrid.com",
    "45.download.real-debrid.cloud",
]

SPEEDTEST_PATHS = ("/speedtest/test.rar", "/speedtest/testDefault.rar")


def _normalize_host(raw: str) -> str:
    h = raw.strip().lower()
    if not h:
        return ""
    if "real-debrid" in h or h.endswith(".rdeb.io"):
        return h
    return f"{h}.download.real-debrid.com"


def _mbps(bytes_read: int, elapsed: float) -> float:
    if elapsed <= 0 or bytes_read <= 0:
        return 0.0
    return (bytes_read * 8) / elapsed / 1_000_000


def _probe_host(host: str, seconds: float, connect_to: float) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "apu-rd-cdn-speedtest/1.0"
    last_err = ""
    for path in SPEEDTEST_PATHS:
        url = f"https://{host}{path}"
        started = time.monotonic()
        bytes_read = 0
        try:
            with session.get(url, stream=True, timeout=(connect_to, seconds + 5)) as r:
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    bytes_read += len(chunk)
                    if time.monotonic() - started >= seconds:
                        break
        except requests.RequestException as e:
            last_err = str(e)[:120]
            continue

        elapsed = time.monotonic() - started
        if bytes_read > 0 and elapsed > 0:
            return {
                "host": host,
                "ok": True,
                "mbps": _mbps(bytes_read, elapsed),
                "mib_s": bytes_read / elapsed / (1024 * 1024),
                "bytes": bytes_read,
                "seconds": round(elapsed, 2),
                "path": path,
            }
        last_err = last_err or "no data"

    return {"host": host, "ok": False, "error": last_err or "unreachable"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Real-Debrid CDN download hosts.")
    parser.add_argument(
        "--hosts",
        help="Comma-separated hosts (short names ok: nyk7-4, den1-4). Default: built-in list.",
    )
    parser.add_argument("--seconds", type=float, default=6.0, help="Seconds to download per host.")
    parser.add_argument("--workers", type=int, default=8, help="Parallel probes.")
    parser.add_argument("--top", type=int, default=20, help="How many results to print.")
    parser.add_argument("--connect-timeout", type=float, default=8.0)
    args = parser.parse_args()

    if args.hosts:
        hosts = [_normalize_host(h) for h in re.split(r"[,;\s]+", args.hosts) if h.strip()]
    else:
        hosts = list(DEFAULT_HOSTS)

    hosts = list(dict.fromkeys(h for h in hosts if h))
    if not hosts:
        print("No hosts to test.", file=sys.stderr)
        return 1

    print(f"Testing {len(hosts)} host(s) for ~{args.seconds}s each ({args.workers} workers)…\n")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {
            pool.submit(_probe_host, host, args.seconds, args.connect_timeout): host
            for host in hosts
        }
        for fut in as_completed(futs):
            results.append(fut.result())

    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    ok.sort(key=lambda r: r["mbps"], reverse=True)

    print(f"{'HOST':<42} {'MiB/s':>8} {'Mbps':>8}  NOTE")
    print("-" * 72)
    for r in ok[: max(1, args.top)]:
        print(
            f"{r['host']:<42} {r['mib_s']:8.2f} {r['mbps']:8.1f}  "
            f"{r['bytes'] // (1024 * 1024)} MiB in {r['seconds']}s"
        )

    if fail:
        print(f"\nFailed ({len(fail)}):")
        for r in sorted(fail, key=lambda x: x["host"])[:10]:
            print(f"  {r['host']}: {r.get('error', '?')}")
        if len(fail) > 10:
            print(f"  … and {len(fail) - 10} more")

    if ok:
        best = ok[0]["host"]
        short = best.removesuffix(".download.real-debrid.com").removesuffix(".download.real-debrid.cloud")
        print(f"\nSuggested .env (fastest from this VPS):")
        print(f"REAL_DEBRID_PREFERRED_CDN={short}")
    else:
        print("\nNo host returned data. Try --hosts with nodes you know work.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
