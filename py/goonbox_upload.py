"""GoonBox image upload client (https://goonbox.cr/plugin — Direct API).

Server-side uploads support either:

1. **Bearer token** — ``GOONBOX_AUTH_TOKEN`` (only if present in browser
   ``localStorage.authToken``; many accounts use session login only).
2. **Session cookies** — ``GOONBOX_SESSION`` + ``GOONBOX_XSRF_TOKEN`` copied
   from DevTools while logged in on goonbox.cr (expires ~2 h).

Session POSTs also require ``Origin`` (and ``Referer`` for upload) — Laravel
Sanctum returns 401 without them even when cookies are valid.

Optional ``album_id`` is sent as a form field on POST /api/upload.
"""
from __future__ import annotations

import os
import re
import struct
import urllib.parse
import zlib

import requests


GOONBOX_BASE_URL = (os.environ.get("GOONBOX_BASE_URL") or "https://goonbox.cr").rstrip("/")
GOONBOX_AUTH_TOKEN = (os.environ.get("GOONBOX_AUTH_TOKEN") or "").strip()
GOONBOX_SESSION = (os.environ.get("GOONBOX_SESSION") or "").strip()
GOONBOX_XSRF_TOKEN = (os.environ.get("GOONBOX_XSRF_TOKEN") or "").strip()
# Optional: paste the full Cookie header from DevTools (overrides SESSION/XSRF above).
GOONBOX_COOKIE = (os.environ.get("GOONBOX_COOKIE") or "").strip()
GOONBOX_ALBUM_ID = (os.environ.get("GOONBOX_ALBUM_ID") or "").strip()
GOONBOX_BBCODE_SIMPLE = (os.environ.get("GOONBOX_BBCODE_SIMPLE") or "1").strip().lower() in (
    "1", "true", "yes", "on",
)


def configured() -> bool:
    return bool(GOONBOX_AUTH_TOKEN) or bool(GOONBOX_SESSION) or bool(GOONBOX_COOKIE)


def auth_mode() -> str:
    if GOONBOX_AUTH_TOKEN:
        return "bearer"
    if GOONBOX_COOKIE or GOONBOX_SESSION:
        return "session"
    return "none"


def _probe_png_bytes() -> bytes:
    """Small but valid PNG (GoonBox rejects the minimal 1×1 probe)."""
    w, h = 10, 10
    raw = b"".join(b"\x00" + bytes([255, 0, 0, 255] * w) for _ in range(h))
    comp = zlib.compress(raw, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", comp)
        + chunk(b"IEND", b"")
    )


_PROBE_PNG = _probe_png_bytes()


def _decode_xsrf(raw: str) -> str:
    return urllib.parse.unquote((raw or "").strip())


def _session_headers(xsrf_header: str = "", *, for_upload: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json", "Origin": GOONBOX_BASE_URL}
    if xsrf_header:
        headers["X-XSRF-TOKEN"] = xsrf_header
    if for_upload:
        headers["Referer"] = f"{GOONBOX_BASE_URL}/upload"
    return headers


def _cookie_header(session_value: str = "", xsrf_value: str = "") -> str:
    if GOONBOX_COOKIE:
        return GOONBOX_COOKIE
    parts: list[str] = []
    if session_value:
        parts.append(f"goonbox_session={session_value}")
    if xsrf_value:
        parts.append(f"XSRF-TOKEN={xsrf_value}")
    return "; ".join(parts)


def _xsrf_header_value(session_value: str = "", xsrf_value: str = "") -> str:
    if xsrf_value:
        return _decode_xsrf(xsrf_value)
    if GOONBOX_XSRF_TOKEN and not session_value:
        return _decode_xsrf(GOONBOX_XSRF_TOKEN)
    if GOONBOX_XSRF_TOKEN and session_value:
        return _decode_xsrf(GOONBOX_XSRF_TOKEN)
    m = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", GOONBOX_COOKIE)
    if m:
        return _decode_xsrf(m.group(1))
    return ""


def _session_upload_headers(session_value: str = "", xsrf_value: str = "") -> dict[str, str]:
    xsrf = _xsrf_header_value(session_value, xsrf_value)
    headers = _session_headers(xsrf, for_upload=True)
    cookie = _cookie_header(session_value, xsrf_value)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def upload_config() -> dict:
    """Public upload limits from GET /api/upload/config."""
    try:
        r = requests.get(
            f"{GOONBOX_BASE_URL}/api/upload/config",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
        if isinstance(body, dict):
            return body
    except requests.RequestException:
        pass
    return {"max_bytes": 26_214_400, "uploads_enabled": True}


def probe_session(
    *,
    bearer: str = "",
    session_value: str = "",
    xsrf_value: str = "",
    cookie_header: str = "",
) -> dict:
    """Check GET /api/auth/me — validates session before attempting upload."""
    headers = _session_headers(_xsrf_header_value(session_value, xsrf_value))
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif cookie_header or session_value or GOONBOX_COOKIE:
        headers["Cookie"] = cookie_header or _cookie_header(session_value, xsrf_value)
    else:
        return {"ok": False, "status": None, "message": "No credentials", "user": None}

    try:
        r = requests.get(f"{GOONBOX_BASE_URL}/api/auth/me", headers=headers, timeout=30)
    except requests.RequestException as e:
        return {"ok": False, "status": None, "message": str(e), "user": None}

    body = None
    try:
        body = r.json()
    except ValueError:
        body = None
    user = body.get("user") if isinstance(body, dict) else None
    return {
        "ok": bool(user),
        "status": r.status_code,
        "message": (user or {}).get("username") if user else "Not logged in",
        "user": user,
    }


def bbcode_from_response(raw: dict) -> str:
    """Extract a single-line BBCode fragment from a GoonBox upload JSON body."""
    if not isinstance(raw, dict):
        raise RuntimeError(f"Unexpected GoonBox response: {raw!r}")

    embed = raw.get("embed")
    if isinstance(embed, dict):
        if GOONBOX_BBCODE_SIMPLE:
            direct = (embed.get("direct") or embed.get("medium") or "").strip()
            if direct:
                return f"[IMG]{direct}[/IMG]"
        bb = (embed.get("bbcode") or "").strip()
        if bb:
            return re.sub(r"\s+", "", bb)

    image = raw.get("image")
    if isinstance(image, dict):
        direct = (image.get("original_url") or image.get("medium_url") or "").strip()
        if direct:
            if GOONBOX_BBCODE_SIMPLE:
                return f"[IMG]{direct}[/IMG]"
            img_id = image.get("encoded_id") or image.get("id") or ""
            medium = (image.get("medium_url") or direct).strip()
            if img_id:
                page = f"{GOONBOX_BASE_URL}/img/{img_id}"
                return f"[url={page}][img]{medium}[/img][/url]"
            return f"[IMG]{direct}[/IMG]"

    raise RuntimeError(f"GoonBox upload returned no embed/image URLs: {raw!r}")


def _post_upload(
    files: dict,
    form: dict,
    *,
    bearer: str = "",
    session_value: str = "",
    xsrf_value: str = "",
    cookie_header: str = "",
) -> requests.Response:
    """Low-level upload POST for auth probing."""
    if bearer:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {bearer}"}
        return requests.post(
            f"{GOONBOX_BASE_URL}/api/upload",
            headers=headers,
            files=files,
            data=form,
            timeout=180,
        )

    cookie = cookie_header or _cookie_header(session_value, xsrf_value)
    if not cookie and not GOONBOX_COOKIE:
        raise RuntimeError("No auth credentials supplied")

    headers = _session_upload_headers(session_value, xsrf_value)
    if cookie_header:
        headers["Cookie"] = cookie_header

    return requests.post(
        f"{GOONBOX_BASE_URL}/api/upload",
        headers=headers,
        files=files,
        data=form,
        timeout=180,
    )


def probe_auth(
    *,
    bearer: str = "",
    session_value: str = "",
    xsrf_value: str = "",
    cookie_header: str = "",
    label: str = "",
) -> dict:
    """Try a tiny upload with the given credentials. Does not mutate env."""
    if not bearer and not session_value and not cookie_header and not GOONBOX_COOKIE:
        return {
            "label": label or "no credentials",
            "ok": False,
            "status": None,
            "message": "No bearer token or session cookie supplied",
        }

    me = probe_session(
        bearer=bearer,
        session_value=session_value,
        xsrf_value=xsrf_value,
        cookie_header=cookie_header,
    )

    files = {"file": ("apu_auth_probe.png", _PROBE_PNG, "image/png")}
    form: dict = {}
    album = (GOONBOX_ALBUM_ID or "").strip()
    if album and album.lower() not in ("none", ""):
        form["album_id"] = album
    try:
        r = _post_upload(
            files,
            form,
            bearer=bearer,
            session_value=session_value,
            xsrf_value=xsrf_value,
            cookie_header=cookie_header,
        )
    except requests.RequestException as e:
        return {
            "label": label or "custom",
            "ok": False,
            "status": None,
            "message": str(e),
            "session_ok": me.get("ok"),
            "session_user": me.get("message"),
        }

    body: dict | str | None
    try:
        body = r.json()
    except ValueError:
        body = (r.text or "")[:500]

    ok = 200 <= r.status_code < 300
    message = ""
    if isinstance(body, dict):
        message = str(body.get("message") or "")
    elif isinstance(body, str):
        message = body[:200]

    result = {
        "label": label or "custom",
        "ok": ok,
        "status": r.status_code,
        "message": message,
        "session_ok": me.get("ok"),
        "session_user": me.get("message"),
    }
    if ok and isinstance(body, dict):
        try:
            result["bbcode"] = bbcode_from_response(body)
        except RuntimeError:
            pass
    return result


def probe_all_configured() -> list[dict]:
    """Run auth probes for env-configured credentials."""
    results: list[dict] = []

    if GOONBOX_AUTH_TOKEN:
        results.append(probe_auth(bearer=GOONBOX_AUTH_TOKEN, label="GOONBOX_AUTH_TOKEN (Bearer)"))

    if GOONBOX_COOKIE:
        results.append(probe_auth(cookie_header=GOONBOX_COOKIE, label="GOONBOX_COOKIE (full header)"))
    elif GOONBOX_SESSION:
        results.append(
            probe_auth(
                session_value=GOONBOX_SESSION,
                xsrf_value=GOONBOX_XSRF_TOKEN,
                label="GOONBOX_SESSION + GOONBOX_XSRF_TOKEN",
            )
        )

    if not results:
        results.append(probe_auth(label="no credentials"))
    return results


def upload_bytes(
    data: bytes,
    filename: str,
    *,
    content_type: str = "application/octet-stream",
    album_id: str | None = None,
) -> dict:
    """Upload in-memory file bytes. Returns parsed JSON + ``bbcode`` key."""
    if not data:
        raise RuntimeError("empty upload payload")
    if not configured():
        raise RuntimeError(
            "GoonBox auth not configured — set GOONBOX_AUTH_TOKEN or GOONBOX_SESSION + GOONBOX_XSRF_TOKEN"
        )

    album = (album_id if album_id is not None else GOONBOX_ALBUM_ID).strip()
    files = {"file": (filename, data, content_type)}
    form: dict = {}
    if album and album.lower() not in ("none", ""):
        form["album_id"] = album

    if GOONBOX_AUTH_TOKEN:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {GOONBOX_AUTH_TOKEN}"}
        r = requests.post(
            f"{GOONBOX_BASE_URL}/api/upload",
            headers=headers,
            files=files,
            data=form,
            timeout=180,
        )
    else:
        r = requests.post(
            f"{GOONBOX_BASE_URL}/api/upload",
            headers=_session_upload_headers(),
            files=files,
            data=form,
            timeout=180,
        )

    if r.status_code == 401:
        raise RuntimeError(
            "GoonBox rejected credentials (401) — refresh cookies or check Origin/session expiry"
        )
    if r.status_code == 419:
        raise RuntimeError(
            "GoonBox CSRF/session expired (419) — refresh GOONBOX_SESSION and GOONBOX_XSRF_TOKEN from the browser"
        )
    if r.status_code == 413:
        raise RuntimeError("File exceeds GoonBox upload size limit")
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected GoonBox response: {body!r}")
    body = dict(body)
    body["bbcode"] = bbcode_from_response(body)
    return body
