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
from realdebrid import DEFAULT_HOSTS, benchmark_hosts, normalize_cdn_host


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
        hosts = [normalize_cdn_host(h) for h in re.split(r"[,;\s]+", args.hosts) if h.strip()]
    else:
        hosts = list(DEFAULT_HOSTS)

    hosts = list(dict.fromkeys(h for h in hosts if h))
    if not hosts:
        print("No hosts to test.", file=sys.stderr)
        return 1

    print(f"Testing {len(hosts)} host(s) for ~{args.seconds}s each ({args.workers} workers)…\n")

    results = benchmark_hosts(
        hosts,
        seconds=args.seconds,
        workers=args.workers,
        connect_to=args.connect_timeout,
    )

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
        print(f"\nSuggested .env (fastest from this VPS):")
        print(f"REAL_DEBRID_PREFERRED_CDN={best}")
    else:
        print("\nNo host returned data. Try --hosts with nodes you know work.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
