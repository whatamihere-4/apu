"""
Resolve Real-Debrid panel links to geo-routed CDN download URLs.

Panel links from the torrents UI (``https://real-debrid.com/d/ID``) route through a
generic gateway. The API ``/unrestrict/link`` returns a ``download`` field pointing
at the nearest CDN node (``https://den1-4.download.real-debrid.com/d/OTHER_ID/file``),
matching what Stremio/Torrentio obtain when resolving streams client-side.
"""
from __future__ import annotations

import os
import re
from typing import Callable
from urllib.parse import urlparse, urlunparse

import requests

API_BASE = (os.environ.get("REAL_DEBRID_API_BASE") or "https://api.real-debrid.com/rest/1.0").rstrip("/")
API_TOKEN = (os.environ.get("REAL_DEBRID_API_TOKEN") or "").strip()
REMOTE = (os.environ.get("REAL_DEBRID_REMOTE") or "0").strip().lower() in ("1", "true", "yes", "on")
_PREFERRED_CDN_RAW = (os.environ.get("REAL_DEBRID_PREFERRED_CDN") or "").strip()

_CONNECT_TO = int(os.environ.get("REAL_DEBRID_CONNECT_TIMEOUT_SEC", "15"))
_READ_TO = int(os.environ.get("REAL_DEBRID_READ_TIMEOUT_SEC", "60"))

_PANEL_LINK_RE = re.compile(
    r"^https?://(?:www\.)?real-debrid\.com/d/([A-Za-z0-9]+)/?(?:\?.*)?$",
    re.IGNORECASE,
)

_CDN_HOST_RE = re.compile(
    r"(?:^|\.)("
    r"download\.real-debrid\.(?:com|cloud)"
    r"|(?:f)?cdn\.real-debrid\.com"
    r"|rdeb\.io"
    r")$",
    re.IGNORECASE,
)


class RealDebridError(RuntimeError):
    """Real-Debrid API or link-resolution failure."""


def is_panel_link(url: str) -> bool:
    """True for ``https://real-debrid.com/d/<id>`` style links."""
    return bool(_PANEL_LINK_RE.match((url or "").strip()))


def is_cdn_link(url: str) -> bool:
    """True when the URL already targets a Real-Debrid CDN host."""
    raw = (url or "").strip()
    if not raw:
        return False
    host = (urlparse(raw).hostname or "").lower()
    if not host:
        return False
    return bool(_CDN_HOST_RE.search(host))


def needs_resolution(url: str) -> bool:
    return is_panel_link(url) and not is_cdn_link(url)


def _normalize_cdn_host(host: str) -> str:
    """Accept ``nyk7-4`` or full ``nyk7-4.download.real-debrid.com``."""
    h = (host or "").strip().lower()
    if not h:
        return ""
    if "real-debrid" in h or h.endswith(".rdeb.io"):
        return h
    return f"{h}.download.real-debrid.com"


def preferred_cdn_hosts() -> list[str]:
    """Hosts from ``REAL_DEBRID_PREFERRED_CDN`` (comma/space separated)."""
    if not _PREFERRED_CDN_RAW:
        return []
    parts = re.split(r"[,;\s]+", _PREFERRED_CDN_RAW)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        norm = _normalize_cdn_host(part)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def apply_preferred_cdn(url: str, *, on_log: Callable[[str], None] | None = None) -> str:
    """
    Rewrite the CDN hostname on an unrestricted link.

    Real-Debrid mirrors the same ``/d/<id>/file`` path on every CDN node; zurg and
    similar tools pick a fast node the same way.
    """
    raw = (url or "").strip()
    if not raw or not is_cdn_link(raw):
        return raw

    prefs = preferred_cdn_hosts()
    if not prefs:
        return raw

    parsed = urlparse(raw)
    orig = (parsed.hostname or "").lower()
    pref = prefs[0]
    if orig == pref:
        return raw

    netloc = pref
    if parsed.port:
        netloc = f"{pref}:{parsed.port}"
    rewritten = urlunparse(parsed._replace(netloc=netloc))
    _log(f"[RD] CDN host {orig} → {pref}", on_log)
    return rewritten


def _log(msg: str, on_log: Callable[[str], None] | None) -> None:
    print(msg, flush=True)
    if on_log:
        on_log(msg)


def unrestrict_link(link: str) -> dict:
    """Call POST /unrestrict/link. Raises RealDebridError on failure."""
    if not API_TOKEN:
        raise RealDebridError("REAL_DEBRID_API_TOKEN is not set")

    data: dict[str, str] = {"link": link.strip()}
    if REMOTE:
        data["remote"] = "1"

    try:
        r = requests.post(
            f"{API_BASE}/unrestrict/link",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            data=data,
            timeout=(_CONNECT_TO, _READ_TO),
        )
    except requests.RequestException as e:
        raise RealDebridError(f"Real-Debrid API request failed: {e}") from e

    if r.status_code == 401:
        raise RealDebridError("Real-Debrid API rejected the token (401)")
    if r.status_code == 403:
        raise RealDebridError("Real-Debrid API forbidden (403) — check account status")
    if not r.ok:
        detail = ""
        try:
            body = r.json()
            if isinstance(body, dict):
                detail = str(body.get("error") or "").strip()
        except ValueError:
            detail = (r.text or "").strip()[:300]
        msg = f"Real-Debrid API HTTP {r.status_code}"
        if detail:
            msg = f"{msg}: {detail}"
        raise RealDebridError(msg)

    try:
        payload = r.json()
    except ValueError as e:
        raise RealDebridError("Real-Debrid API returned non-JSON response") from e
    if not isinstance(payload, dict):
        raise RealDebridError("Real-Debrid API returned unexpected payload")
    return payload


def resolve_download_url(url: str, *, on_log: Callable[[str], None] | None = None) -> str:
    """
    Return a direct CDN URL for Real-Debrid panel links; pass through everything else.

    When ``REAL_DEBRID_API_TOKEN`` is unset, panel links are returned unchanged with a
    warning (legacy behaviour — slower gateway routing).
    """
    raw = (url or "").strip()
    if not raw:
        return raw

    if needs_resolution(raw):
        if not API_TOKEN:
            _log(
                "[RD] Panel link detected but REAL_DEBRID_API_TOKEN is not set; "
                "using URL as-is (CDN resolution skipped)",
                on_log,
            )
            return apply_preferred_cdn(raw, on_log=on_log)

        _log(f"[RD] Resolving panel link via API: {raw}", on_log)
        payload = unrestrict_link(raw)
        download = (payload.get("download") or "").strip()
        if not download:
            raise RealDebridError("Real-Debrid API returned no download URL")

        filename = (payload.get("filename") or "").strip()
        host = urlparse(download).hostname or download
        label = f"{host}/…/{filename}" if filename else host
        _log(f"[RD] CDN link: {label}", on_log)
        return apply_preferred_cdn(download, on_log=on_log)

    return apply_preferred_cdn(raw, on_log=on_log)
