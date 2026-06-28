#!/usr/bin/env python3
"""Probe which GoonBox credentials work for POST /api/upload.

Usage (reads .env / environment):

  python scripts/test_goonbox_auth.py

Explicit one-off tests:

  python scripts/test_goonbox_auth.py --bearer 'YOUR_TOKEN'
  python scripts/test_goonbox_auth.py --session 'goonbox_session_value' --xsrf 'XSRF-TOKEN_value'

Copy cookie values from DevTools → Application → Cookies → goonbox.cr while logged in.
Use the *Value* column only (not the cookie name). XSRF-TOKEN is required for session POSTs.

Each successful probe uploads a 1×1 PNG named apu_auth_probe.png — delete it from your
GoonBox gallery if you do not want it.
"""
from __future__ import annotations

import argparse
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(APP_DIR, "py")
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

import goonbox_upload  # noqa: E402


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


def _print_result(r: dict) -> None:
    status = r.get("status")
    status_s = str(status) if status is not None else "—"
    mark = "OK" if r.get("ok") else "FAIL"
    print(f"[{mark}] {r.get('label', '?')} → HTTP {status_s}")
    if r.get("session_user") is not None:
        sess_mark = "logged in" if r.get("session_ok") else "not logged in"
        print(f"      session (/auth/me): {sess_mark} ({r.get('session_user')})")
    if r.get("message"):
        print(f"      {r['message']}")
    if r.get("bbcode"):
        print(f"      bbcode: {r['bbcode'][:120]}...")
    if goonbox_upload.GOONBOX_ALBUM_ID:
        print(f"      album_id: {goonbox_upload.GOONBOX_ALBUM_ID}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test GoonBox upload authentication")
    parser.add_argument("--bearer", help="Bearer token (localStorage authToken, if you have one)")
    parser.add_argument("--session", help="goonbox_session cookie value")
    parser.add_argument("--xsrf", help="XSRF-TOKEN cookie value")
    parser.add_argument(
        "--album-id",
        help="album_id form field (slug from https://goonbox.cr/a/<slug>, e.g. ajDeMe)",
    )
    parser.add_argument(
        "--no-env",
        action="store_true",
        help="Do not load .env; only use explicit --bearer / --session flags",
    )
    args = parser.parse_args()

    if not args.no_env:
        _load_dotenv()
        # Re-read module constants after dotenv
        import importlib
        importlib.reload(goonbox_upload)

    if args.album_id:
        os.environ["GOONBOX_ALBUM_ID"] = args.album_id.strip()
        import importlib
        importlib.reload(goonbox_upload)

    results: list[dict] = []

    if args.bearer or args.session:
        if args.bearer:
            results.append(
                goonbox_upload.probe_auth(bearer=args.bearer, label="CLI --bearer")
            )
        if args.session:
            results.append(
                goonbox_upload.probe_auth(
                    session_value=args.session,
                    xsrf_value=args.xsrf or "",
                    label="CLI --session" + (" + --xsrf" if args.xsrf else " (missing --xsrf)"),
                )
            )
    else:
        print(f"GoonBox base: {goonbox_upload.GOONBOX_BASE_URL}")
        print(f"Auth mode from env: {goonbox_upload.auth_mode()}")
        print()
        results = goonbox_upload.probe_all_configured()

    any_ok = False
    for r in results:
        _print_result(r)
        any_ok = any_ok or bool(r.get("ok"))
        print()

    if not any_ok:
        print(
            "None of the credentials worked.\n"
            "\n"
            "GoonBox supports two upload auth paths (see goonbox.cr upload page JS):\n"
            "  • Bearer  — Authorization: Bearer <token>  (localStorage authToken; often empty)\n"
            "  • Session — Cookie goonbox_session + header X-XSRF-TOKEN from XSRF-TOKEN cookie\n"
            "\n"
            "If you only see session cookies, set in .env:\n"
            "  GOONBOX_SESSION=<goonbox_session value>\n"
            "  GOONBOX_XSRF_TOKEN=<XSRF-TOKEN value>\n"
            "\n"
            "Session POSTs require Origin/Referer (Sanctum). A 401 with valid cookies\n"
            "usually means those headers were missing — re-run after updating apu.\n"
            "\n"
            "Session cookies expire (~2 hours); Bearer is better for automation if available."
        )
        return 1

    print("At least one method succeeded — use that in .env for GOONBOX_AUTO_UPLOAD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
