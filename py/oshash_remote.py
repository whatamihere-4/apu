"""
Remote OSHASH: HTTP Range fetch + Stash-compatible OpenSubtitles hash.

Matches _compute_oshash in hasher_http.py (64KB head + 64KB tail + size mod 2**64).
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import struct
from typing import Any
from urllib.parse import urlparse, urljoin

import requests

from mp4_iso_duration import (
    duration_seconds_from_iso_buffer,
    duration_seconds_from_suffix_buffer,
)

OSHASH_CHUNK = 65536

_CONTENT_RANGE_TOTAL = re.compile(r"bytes\s+\d+-\d+/(\d+)")
_MAX_REDIRECTS = 10

# Max suffix bytes to Range-fetch when probing `moov` beyond head/tail (default 2 MiB).
_DEFAULT_DURATION_PROBE_MAX = 2097152

# Connect timeout, read timeout (seconds)
_DEFAULT_TIMEOUT = (
    int(os.environ.get("OSHASH_REMOTE_CONNECT_TIMEOUT_SEC", "15")),
    int(os.environ.get("OSHASH_REMOTE_READ_TIMEOUT_SEC", "60")),
)


def _duration_probe_cap() -> int:
    raw = os.environ.get("OSHASH_REMOTE_DURATION_PROBE_MAX_BYTES", "")
    if raw.strip():
        try:
            return max(OSHASH_CHUNK + 1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_DURATION_PROBE_MAX


def _suffix_probe_lengths(size: int, cap: int) -> list[int]:
    """Increasing suffix sizes for ISO BMFF duration probes (after head/tail)."""
    seen: set[int] = set()
    out: list[int] = []
    for cand in (262144, 1048576, cap):
        pl = min(cand, size)
        if pl <= OSHASH_CHUNK or pl < 16:
            continue
        if pl not in seen:
            seen.add(pl)
            out.append(pl)
    return sorted(out)


def _extract_mp4_duration_iso_bmff(
    session: requests.Session,
    final_url: str,
    size: int,
    head: bytes,
    tail: bytes,
    timeout: tuple[int, int],
) -> tuple[float | None, str | None]:
    """
    Best-effort MP4 duration from mvhd. Extra Range GETs only if moov is not in head/tail.
    Returns (seconds, detail tag) or (None, None).
    """
    d = duration_seconds_from_iso_buffer(head)
    if d is not None:
        return d, "mvhd_head"

    d = duration_seconds_from_suffix_buffer(tail)
    if d is not None:
        return d, "mvhd_tail"

    cap = _duration_probe_cap()
    for plen in _suffix_probe_lengths(size, cap):
        try:
            start = size - plen
            suf = _fetch_range(session, final_url, start, size - 1, timeout)
        except OSHashRemoteError:
            continue
        d = duration_seconds_from_suffix_buffer(suf)
        if d is not None:
            return d, "mvhd_suffix"

    return None, None


class OSHashRemoteError(Exception):
    """User-facing fetch or validation error."""


def compute_oshash_from_chunks(size: int, head: bytes, tail: bytes) -> str:
    """Stash-compatible OpenSubtitles hash (same as hasher_http._compute_oshash)."""
    if size < OSHASH_CHUNK:
        raise ValueError(f"file too small for oshash ({size} < {OSHASH_CHUNK} bytes)")
    if len(head) != OSHASH_CHUNK or len(tail) != OSHASH_CHUNK:
        raise ValueError("head and tail must each be exactly 64KiB")
    h = size
    chunks = OSHASH_CHUNK // 8
    fmt = "<" + "Q" * chunks
    head_vals = struct.unpack_from(fmt, head, 0)
    tail_vals = struct.unpack_from(fmt, tail, 0)
    for v in head_vals:
        h += v
    for v in tail_vals:
        h += v
    h &= (1 << 64) - 1
    return f"{h:016x}"


def _ensure_public_hostname(hostname: str) -> None:
    if not hostname:
        raise OSHashRemoteError("missing hostname")
    hn = hostname.strip().lower()
    try:
        infos = socket.getaddrinfo(hn, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise OSHashRemoteError(f"cannot resolve host: {e}") from e
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if not ip.is_global:
            raise OSHashRemoteError(f"host resolves to non-public IP {ip_str}")


def _validate_initial_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise OSHashRemoteError("URL is required")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise OSHashRemoteError("only http and https URLs are allowed")
    if not parsed.hostname:
        raise OSHashRemoteError("invalid URL: missing host")
    _ensure_public_hostname(parsed.hostname)
    return raw


def _request_no_redirect(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: tuple[int, int],
    **kwargs: Any,
) -> tuple[str, requests.Response]:
    """Follow redirects manually; verify each hop host resolves to a public IP."""
    current = url
    for _ in range(_MAX_REDIRECTS):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            raise OSHashRemoteError("redirect to non-http(s) URL rejected")
        _ensure_public_hostname(parsed.hostname)
        r = session.request(method, current, allow_redirects=False, timeout=timeout, **kwargs)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            current = urljoin(current, r.headers["Location"])
            continue
        return current, r
    raise OSHashRemoteError("too many redirects")


def _parse_total_from_content_range(value: str | None) -> int | None:
    if not value:
        return None
    m = _CONTENT_RANGE_TOTAL.search(value)
    if not m:
        return None
    return int(m.group(1))


def _infer_size(session: requests.Session, url: str, timeout: tuple[int, int]) -> tuple[str, int]:
    """Return (final_url, total_size_in_bytes)."""
    final, r = _request_no_redirect(session, "HEAD", url, timeout=timeout)
    if r.status_code == 404:
        raise OSHashRemoteError("HTTP 404 — URL not found")
    if r.status_code == 200 and r.headers.get("Content-Length"):
        try:
            return final, int(r.headers["Content-Length"])
        except ValueError:
            pass

    # Probe with minimal range GET
    final, r = _request_no_redirect(
        session,
        "GET",
        url,
        timeout=timeout,
        headers={"Range": "bytes=0-0"},
    )
    if r.status_code == 206:
        total = _parse_total_from_content_range(r.headers.get("Content-Range"))
        if total is not None:
            return final, total

    final, r = _request_no_redirect(
        session,
        "GET",
        url,
        timeout=timeout,
        headers={"Range": f"bytes=0-{OSHASH_CHUNK - 1}"},
    )
    if r.status_code == 206:
        total = _parse_total_from_content_range(r.headers.get("Content-Range"))
        if total is not None:
            return final, total

    raise OSHashRemoteError(
        "could not determine file size (HEAD without Content-Length and Range probe failed); "
        "server may not support Range requests"
    )


def _fetch_range(
    session: requests.Session,
    final_url: str,
    start: int,
    end_inclusive: int,
    timeout: tuple[int, int],
) -> bytes:
    rng = f"bytes={start}-{end_inclusive}"
    _final, r = _request_no_redirect(
        session,
        "GET",
        final_url,
        timeout=timeout,
        headers={"Range": rng},
    )
    if r.status_code != 206:
        raise OSHashRemoteError(
            f"expected 206 Partial Content for Range {rng}, got HTTP {r.status_code}"
        )
    data = r.content
    expected = end_inclusive - start + 1
    if len(data) != expected:
        raise OSHashRemoteError(
            f"range response length mismatch: got {len(data)} bytes, expected {expected}"
        )
    return data


def fetch_oshash_from_url(url: str, timeout: tuple[int, int] | None = None) -> dict[str, Any]:
    """
    Fetch head/tail via HTTP Range and compute OSHASH.

    Returns dict with keys: ok (bool), hash, size_bytes, final_url, detail (error text if ok false).
    """
    timeout = timeout or _DEFAULT_TIMEOUT
    try:
        _validate_initial_url(url)
    except OSHashRemoteError as e:
        return {"ok": False, "detail": str(e)}

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "gofup-oshash-remote/1.0",
            "Accept": "*/*",
        }
    )

    try:
        final_url, size = _infer_size(session, url.strip(), timeout)
        if size < OSHASH_CHUNK:
            raise OSHashRemoteError(
                f"file too small for OSHASH ({size} < {OSHASH_CHUNK} bytes)"
            )

        head = _fetch_range(session, final_url, 0, OSHASH_CHUNK - 1, timeout)
        tail_start = size - OSHASH_CHUNK
        tail = _fetch_range(session, final_url, tail_start, size - 1, timeout)
        h = compute_oshash_from_chunks(size, head, tail)

        out: dict[str, Any] = {
            "ok": True,
            "hash": h,
            "size_bytes": size,
            "final_url": final_url,
            "range_requests": True,
        }
        dur_sec, dur_tag = _extract_mp4_duration_iso_bmff(
            session, final_url, size, head, tail, timeout
        )
        if dur_sec is not None and dur_sec >= 0:
            out["duration"] = dur_sec
            out["duration_int"] = int(dur_sec)
            out["duration_detail"] = dur_tag or "mvhd"
        return out
    except OSHashRemoteError as e:
        return {"ok": False, "detail": str(e)}
    except ValueError as e:
        return {"ok": False, "detail": str(e)}
    except requests.RequestException as e:
        return {"ok": False, "detail": f"HTTP error: {e}"}

