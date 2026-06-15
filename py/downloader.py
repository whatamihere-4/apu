import os
import re
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.exceptions import IncompleteRead, ProtocolError
from urllib.parse import urlparse, unquote

import requests.exceptions as req_exc

DOWNLOADS_DIR = "/downloads"


class TransferCancelled(Exception):
    """Raised when the user cancels an in-flight download (see should_cancel)."""


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _make_session(*, identity_encoding: bool = False):
    s = requests.Session()
    h = dict(HEADERS)
    if identity_encoding:
        # Avoid gzip/br so Content-Length matches bytes written (needed for Range resume).
        h["Accept-Encoding"] = "identity"
    s.headers.update(h)
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _transient_download_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (req_exc.ChunkedEncodingError, req_exc.ConnectionError, req_exc.Timeout),
    ):
        return True
    if isinstance(exc, ProtocolError):
        return True
    cur: BaseException | None = exc
    for _ in range(12):
        if cur is None:
            break
        if isinstance(cur, IncompleteRead):
            return True
        cur = cur.__cause__ or cur.__context__
    low = str(exc).lower()
    if "incompleteread" in low or "connection broken" in low:
        return True
    return False


def _parse_content_range_total(value: str) -> int | None:
    if not value:
        return None
    m = re.search(r"/(\d+)\s*$", value.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def download_file(url, on_progress=None, should_cancel=None, on_log=None):
    """
    Stream-download a URL to /downloads with HTTP Range resume on transient cuts.

    Returns the local filepath on success.
    Raises on unrecoverable HTTP/I/O errors.

    should_cancel: optional callable returning True when the download must abort.
    on_log: optional one-arg callable receiving short status lines (for job UI).
    """
    from gofile_upload import format_size

    def log(msg: str) -> None:
        print(msg, flush=True)
        if on_log:
            on_log(msg)

    connect_to = _env_int("DOWNLOAD_CONNECT_TIMEOUT_SEC", 30)
    read_to = _env_int("DOWNLOAD_READ_TIMEOUT_SEC", 900)
    max_attempts = max(1, _env_int("DOWNLOAD_RESUME_ATTEMPTS", 16))
    chunk_size = max(4096, _env_int("DOWNLOAD_CHUNK_SIZE", 65536))

    session = _make_session(identity_encoding=True)

    log(f"[DL] Requesting (resume up to {max_attempts} pass(es), identity encoding): {url}")

    filepath: str | None = None
    final_url = url
    total = 0

    attempt = 0
    last_err: BaseException | None = None
    r = None

    def _close_r() -> None:
        nonlocal r
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
            r = None

    while attempt < max_attempts:
        attempt += 1
        existing = os.path.getsize(filepath) if filepath and os.path.isfile(filepath) else 0
        headers = {}
        if filepath and existing > 0:
            headers["Range"] = f"bytes={existing}-"

        try:
            r = session.get(
                final_url if filepath else url,
                stream=True,
                allow_redirects=True,
                timeout=(connect_to, read_to),
                headers=headers or None,
            )
            if filepath is None:
                final_url = r.url

            if r.status_code == 416:
                if total > 0 and existing >= total:
                    _close_r()
                    log("[DL] HTTP 416 — file already complete.")
                    if on_progress:
                        on_progress(100.0, existing, total, 0, 0)
                    if not filepath:
                        raise RuntimeError("HTTP 416 but no filepath")
                    return filepath
                _close_r()
                log("[DL] HTTP 416 — clearing partial and restarting.")
                try:
                    if filepath:
                        os.unlink(filepath)
                except OSError:
                    pass
                filepath = None
                existing = 0
                continue

            if headers.get("Range"):
                if r.status_code == 206:
                    pass
                elif r.status_code == 200:
                    _close_r()
                    log("[DL] Server ignored Range (200); truncating partial and restarting.")
                    try:
                        if filepath:
                            os.unlink(filepath)
                    except OSError:
                        pass
                    filepath = None
                    continue
                else:
                    r.raise_for_status()
            else:
                if r.status_code not in (200, 206):
                    r.raise_for_status()
                if existing > 0:
                    _close_r()
                    try:
                        if filepath:
                            os.unlink(filepath)
                    except OSError:
                        pass
                    filepath = None
                    existing = 0
                    log("[DL] Fresh download after clearing stale partial.")

            if filepath is None:
                filename = _extract_filename(r) or f"download_{int(time.time())}"
                filepath = _unique_path(os.path.join(DOWNLOADS_DIR, filename))
                try:
                    total = int(r.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    total = 0
                ar = (r.headers.get("Accept-Ranges") or "").strip().lower()
                crt = _parse_content_range_total(r.headers.get("Content-Range") or "")
                if crt is not None:
                    total = crt
                log(
                    f"[DL] Saving as: {filepath}  "
                    f"size={format_size(total) if total else 'unknown'}  "
                    f"Accept-Ranges={'bytes' if ar == 'bytes' else 'no'}"
                )

            if r.status_code == 206:
                crt = _parse_content_range_total(r.headers.get("Content-Range") or "")
                if crt is not None:
                    total = crt
                mode = "ab"
            else:
                mode = "wb"

            downloaded = existing
            start = time.time()
            last_report = 0.0

            try:
                with open(filepath, mode) as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if should_cancel and should_cancel():
                            raise TransferCancelled("Download cancelled")
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if on_progress and now - last_report >= 1.0:
                            last_report = now
                            elapsed = now - start
                            speed = (downloaded - existing) / elapsed if elapsed > 0 else 0
                            if total > 0:
                                pct = (downloaded / total) * 100
                                remaining = ((total - downloaded) / speed) if speed > 0 else 0
                            else:
                                pct = -1.0
                                remaining = -1.0
                            on_progress(pct, downloaded, total, speed, remaining)
            finally:
                _close_r()

            actual_size = os.path.getsize(filepath)
            if total > 0 and actual_size != total:
                log(
                    f"[DL] size mismatch after pass {attempt}: "
                    f"have {format_size(actual_size)} expected {format_size(total)} — will resume."
                )
                last_err = RuntimeError(
                    f"Download incomplete: expected {format_size(total)}, got {format_size(actual_size)}"
                )
                if attempt >= max_attempts:
                    try:
                        os.unlink(filepath)
                    except OSError:
                        pass
                    raise last_err
                continue

            if on_progress:
                on_progress(100.0, actual_size, actual_size if total else actual_size, 0, 0)

            log(f"[DL] {os.path.basename(filepath)} complete ({format_size(actual_size)})")
            return filepath

        except TransferCancelled:
            _close_r()
            try:
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
                    log(f"[DL] Removed partial file after cancel: {filepath}")
            except OSError as e:
                log(f"[DL] Failed to remove partial {filepath}: {e}")
            raise

        except OSError:
            _close_r()
            raise

        except req_exc.RequestException as e:
            last_err = e
            _close_r()
            if not _transient_download_error(e):
                if filepath and os.path.exists(filepath):
                    try:
                        os.unlink(filepath)
                    except OSError:
                        pass
                raise
            log(f"[DL] pass {attempt}/{max_attempts} interrupted ({type(e).__name__}): {e}")
            if attempt >= max_attempts:
                if filepath and os.path.exists(filepath):
                    try:
                        os.unlink(filepath)
                    except OSError:
                        pass
                raise
            time.sleep(min(2.0 * attempt, 15.0))
            continue

    if last_err:
        raise last_err
    raise RuntimeError("Download failed after retries")


def _extract_filename(response):
    cd = response.headers.get("Content-Disposition", "")
    if cd:
        for pattern in [r"filename\*=(?:UTF-8''|utf-8'')(.+)", r'filename="(.+?)"', r"filename=(\S+)"]:
            m = re.search(pattern, cd, re.IGNORECASE)
            if m:
                name = unquote(m.group(1)).strip().rstrip(";")
                if name:
                    return _sanitize(name)
    path = urlparse(response.url).path
    basename = unquote(os.path.basename(path))
    if basename and "." in basename:
        return _sanitize(basename)
    return None


def _sanitize(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip(". ")


def _unique_path(filepath):
    if not os.path.exists(filepath):
        return filepath
    base, ext = os.path.splitext(filepath)
    n = 1
    while True:
        candidate = f"{base}_{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1
