#!/usr/bin/env python3
"""
Build cache/gofile-folders.json by walking folder IDs under ROOT_FOLDER_LINK using the
same JSON endpoint as the website (no paid profile API key).

Uses only stdlib + requests (already used by this project).

From your logged-in (or guest) gofile.io tab:
  1. Open DevTools → Network, reload a folder page.
  2. Pick a request to …/contents/<some-uuid> (status 200).
  3. Copy Request headers into .env:
     - Authorization: Bearer <token>   → GOFILE_ACCOUNT_TOKEN=<token>
     - X-Website-Token: <value>         → GOFILE_X_WEBSITE_TOKEN=<value>
     - User-Agent                       → GOFILE_USER_AGENT=<same string> (recommended)

The cookie value `accountToken` is the same token as the Bearer string after "Bearer ".

Expiry: GoFile can invalidate the session; if you get 401 / error-notPremium, paste
fresh headers from the browser again.
"""

from __future__ import annotations

import json
import os
import re
import sys

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(SCRIPT_DIR)
ENV_PATH = os.path.join(APP_DIR, ".env")
OUT_PATH = os.path.join(APP_DIR, "cache", "gofile-folders.json")

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _parse_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except OSError:
        pass
    return out


def _folder_id_from_link(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    m = re.search(r"/d/([a-f0-9-]{36})", url, re.I)
    if m:
        return m.group(1).lower()
    m2 = re.fullmatch(r"([a-f0-9-]{36})", url, re.I)
    if m2:
        return m2.group(1).lower()
    return None


def _normalize_bearer(raw: str) -> str:
    t = (raw or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def _fetch_content_page(
    session: requests.Session,
    folder_id: str,
    page: int,
    headers: dict[str, str],
) -> dict:
    url = f"https://api.gofile.io/contents/{folder_id}"
    params = {
        "contentFilter": "",
        "page": page,
        "pageSize": 1000,
        "sortField": "name",
        "sortDirection": 1,
    }
    r = session.get(url, params=params, headers=headers, timeout=90)
    r.raise_for_status()
    return r.json()


def _list_folder_merged(
    session: requests.Session,
    folder_id: str,
    headers: dict[str, str],
) -> tuple[str, str, dict]:
    """Returns (name, type, children dict)."""
    merged: dict = {}
    name = ""
    ctype = ""
    page = 1
    while True:
        body = _fetch_content_page(session, folder_id, page, headers)
        status = body.get("status")
        if status != "ok":
            raise RuntimeError(f"contents/{folder_id} page {page}: {status} {body!r}")
        data = body.get("data") or {}
        if page == 1:
            name = data.get("name") or ""
            ctype = data.get("type") or ""
        children = data.get("children") or {}
        merged.update(children)
        if len(children) < 1000:
            break
        page += 1
    return name, ctype, merged


def main() -> int:
    env = _parse_env(ENV_PATH)
    link = (env.get("ROOT_FOLDER_LINK") or "").strip()
    root_id = _folder_id_from_link(link)
    if not root_id:
        print(
            "Set ROOT_FOLDER_LINK in .env to a gofile.io folder URL (…/d/<uuid>).",
            file=sys.stderr,
        )
        return 1

    token = _normalize_bearer(env.get("GOFILE_ACCOUNT_TOKEN") or "")
    wt = (env.get("GOFILE_X_WEBSITE_TOKEN") or "").strip()
    if not token or not wt:
        print(
            "Set GOFILE_ACCOUNT_TOKEN and GOFILE_X_WEBSITE_TOKEN in .env.\n"
            "Copy them from DevTools → Network → a successful GET …/contents/<id> "
            "(Authorization bearer value and X-Website-Token). "
            "The site cookie accountToken matches the bearer token.",
            file=sys.stderr,
        )
        return 1

    ua = (env.get("GOFILE_USER_AGENT") or _DEFAULT_UA).strip()
    lang = (env.get("GOFILE_ACCEPT_LANGUAGE") or "en-US,en;q=0.9").strip()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Website-Token": wt,
        "X-BL": lang,
        "User-Agent": ua,
        "Accept": "application/json",
    }

    session = requests.Session()
    folders: dict[str, str] = {}
    stack = [root_id]
    seen: set[str] = set()

    try:
        while stack:
            fid = stack.pop()
            if fid in seen:
                continue
            seen.add(fid)
            fname, ctype, children = _list_folder_merged(session, fid, headers)
            if ctype == "folder" and fname:
                folders[fid] = fname
            for _cid, item in children.items():
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "folder":
                    continue
                cid = item.get("id")
                if not cid or not isinstance(cid, str):
                    continue
                folders[cid] = item.get("name") or ""
                stack.append(cid)
    except requests.RequestException as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(folders, f, indent=2, sort_keys=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
