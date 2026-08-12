"""PiXhost image upload client (https://pixhost.cc/api — API v2, no auth).

Uploads via ``POST {PIXHOST_API_URL}/images`` and returns ``[IMG]direct[/IMG]``
using the direct ``/images/`` URL (not the show page or thumb).
"""
from __future__ import annotations

import os
import re

import requests


PIXHOST_API_URL = (os.environ.get("PIXHOST_API_URL") or "https://api.pixhost.cc").rstrip("/")
PIXHOST_CONTENT_TYPE = (os.environ.get("PIXHOST_CONTENT_TYPE") or "1").strip()
PIXHOST_MAX_TH_SIZE = (os.environ.get("PIXHOST_MAX_TH_SIZE") or "").strip()
PIXHOST_INCLUDE_MANAGE_URL = (os.environ.get("PIXHOST_INCLUDE_MANAGE_URL") or "0").strip().lower() in (
    "1",
    "true",
    "yes",
)

_SHOW_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?P<host>pixhost\.(?:cc|to)|pixho\.st)/show/(?P<path>.+)$",
    re.I,
)
_THUMB_URL_RE = re.compile(
    r"^https?://t(?P<num>\d+)\.(?P<host>pixhost\.(?:cc|to)|pixho\.st)/thumbs/(?P<path>.+)$",
    re.I,
)


_THUMB_URL_RE = re.compile(
    r"^https?://t(?P<num>\d+)\.(?P<host>pixhost\.(?:cc|to)|pixho\.st)/thumbs/(?P<path>.+)$",
    re.I,
)


def direct_url_from_thumb_url(thumb_url: str) -> str:
    """Derive the full-size direct URL from API ``th_url``.

    PiXhost returns thumb links on ``tN.…/thumbs/…``. The animated/full file is on the
    matching ``imgN.…/images/…`` host (not ``tN.…/images/…``, which serves a static PNG).
    """
    thumb_url = (thumb_url or "").strip()
    m = _THUMB_URL_RE.match(thumb_url)
    if not m:
        return ""
    return f"https://img{m.group('num')}.{m.group('host')}/images/{m.group('path')}"


def direct_url_from_show_url(show_url: str) -> str:
    """Fallback when only ``show_url`` is available (API normally also returns ``th_url``)."""
    show_url = (show_url or "").strip()
    m = _SHOW_URL_RE.match(show_url)
    if not m:
        return ""
    # Without ``th_url`` we cannot know which imgN CDN node holds the file.
    path = m.group("path")
    return f"https://img1.{m.group('host')}/images/{path}"


def direct_url_from_response(raw: dict) -> str:
    """Best direct URL for forum ``[IMG]`` tags from a PiXhost upload JSON body."""
    if not isinstance(raw, dict):
        return ""

    thumb = (raw.get("th_url") or "").strip()
    if thumb:
        direct = direct_url_from_thumb_url(thumb)
        if direct:
            return _normalize_direct_gif_url(direct)

    show = (raw.get("show_url") or "").strip()
    if show:
        direct = direct_url_from_show_url(show)
        if direct:
            return _normalize_direct_gif_url(direct)

    return ""


def _normalize_direct_gif_url(url: str) -> str:
    """Chevereto-style medium GIF previews use ``.md.gif`` (static); strip for animation."""
    return re.sub(r"\.md\.gif$", ".gif", url, flags=re.I)


def bbcode_from_response(raw: dict) -> str:
    """Extract a single-line ``[IMG]`` fragment from a PiXhost upload JSON body."""
    direct = direct_url_from_response(raw)
    if not direct:
        raise RuntimeError(f"PiXhost upload returned no image URLs: {raw!r}")
    return f"[IMG]{direct}[/IMG]"


def upload_bytes(
    data: bytes,
    filename: str,
    *,
    content_type: str = "application/octet-stream",
) -> dict:
    """Upload in-memory file bytes. Returns parsed JSON + ``bbcode`` key."""
    if not data:
        raise RuntimeError("empty upload payload")

    form: dict[str, str] = {
        "content_type": PIXHOST_CONTENT_TYPE or "1",
    }
    if PIXHOST_MAX_TH_SIZE:
        form["max_th_size"] = PIXHOST_MAX_TH_SIZE
    if PIXHOST_INCLUDE_MANAGE_URL:
        form["include_manage_url"] = "1"

    files = {"img": (filename, data, content_type)}
    r = requests.post(
        f"{PIXHOST_API_URL}/images",
        files=files,
        data=form,
        headers={"Accept": "application/json"},
        timeout=180,
    )

    if r.status_code == 413:
        raise RuntimeError("File exceeds PiXhost upload size limit (10 MB)")
    if r.status_code == 414:
        raise RuntimeError("PiXhost rejected file format (unsupported image type)")

    if not r.ok:
        detail = ""
        try:
            err_body = r.json()
            if isinstance(err_body, dict):
                detail = str(
                    err_body.get("message") or err_body.get("error") or err_body.get("detail") or ""
                ).strip()
        except ValueError:
            detail = (r.text or "").strip()[:300]
        msg = f"PiXhost upload HTTP {r.status_code}"
        if detail:
            msg = f"{msg}: {detail}"
        raise RuntimeError(msg)

    body = r.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected PiXhost response: {body!r}")
    body = dict(body)
    body["bbcode"] = bbcode_from_response(body)
    body["direct_url"] = direct_url_from_response(body)
    return body
