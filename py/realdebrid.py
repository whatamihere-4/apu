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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
from urllib.parse import urlparse, urlunparse

import requests

API_BASE = (os.environ.get("REAL_DEBRID_API_BASE") or "https://api.real-debrid.com/rest/1.0").rstrip("/")
API_TOKEN = (os.environ.get("REAL_DEBRID_API_TOKEN") or "").strip()
REMOTE = (os.environ.get("REAL_DEBRID_REMOTE") or "0").strip().lower() in ("1", "true", "yes", "on")
_PREFERRED_CDN_RAW = (os.environ.get("REAL_DEBRID_PREFERRED_CDN") or "").strip()
_RUNTIME_PREFERRED_CDN_RAW = ""

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

# Public defaults used by scripts and optional background scans.
#
# "Numbered pools + named metros (community-maintained lists; not exhaustive)."
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


def normalize_cdn_host(raw: str) -> str:
    """Public wrapper for normalizing speedtest host inputs."""
    return _normalize_cdn_host(raw)


def set_runtime_preferred_cdn(raw: str) -> None:
    """Set preferred CDN hosts for this process (used by background scan)."""
    global _RUNTIME_PREFERRED_CDN_RAW
    _RUNTIME_PREFERRED_CDN_RAW = (raw or "").strip()


def preferred_cdn_hosts() -> list[str]:
    """Preferred CDN hosts (env pin first, then runtime override)."""
    raw = _PREFERRED_CDN_RAW or _RUNTIME_PREFERRED_CDN_RAW
    if not raw:
        return []
    parts = re.split(r"[,;\s]+", raw)
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


def benchmark_hosts(
    hosts: list[str],
    *,
    seconds: float,
    workers: int,
    connect_to: float,
) -> list[dict]:
    """Benchmark Real-Debrid CDN hosts by downloading public speedtest files.

    Returns probe results (ok first, sorted by mbps desc, then failures).
    """
    norm_hosts = []
    seen: set[str] = set()
    for h in hosts:
        norm = normalize_cdn_host(h)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        norm_hosts.append(norm)

    if not norm_hosts:
        return []

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {
            pool.submit(_probe_host, host, seconds, connect_to): host
            for host in norm_hosts
        }
        for fut in as_completed(futs):
            results.append(fut.result())

    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    ok.sort(key=lambda r: r["mbps"], reverse=True)
    return [*ok, *fail]
