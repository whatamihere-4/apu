#!/usr/bin/env python3
"""
HTTP API for the Stash-compatible hasher service.

Endpoints:
  GET  /health, /v1/health
  POST /v1/hash          { filename, algorithm }   — OSHASH | MD5 | PHASH (single JSON response)
  POST /v1/hash_stream   { filename, algorithm } — chunked NDJSON: progress/phase lines, then ``{"type":"result",...}``

Single-shot /v1/hash returns JSON, e.g. for OSHASH:
  { "ok": true, "filename": "...", "algorithm": "OSHASH",
    "hash": "abcdef0123456789", "duration_int": 562 }

OSHASH/MD5 are pure file I/O so they finish in seconds even on large files.
PHASH shells out to the official Stash `phasher` binary (ffmpeg-based, slow).
The hasher image applies a small patch so phasher prints
``STASH_PHASH_PROGRESS <n> <total>`` to stderr after each sprite tile (25 total);
hasher-http turns that into heartbeat ``pct`` / ``eta_sec`` for UIs.

Stdlib only. Mirrors the thumber-http API style for consistency: `filename` is a
basename only; the file must exist at /downloads/<filename> in this container,
which is the same shared mount used by gofup and thumber.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PHASHER_BIN = os.environ.get("HASHER_PHASHER_BIN", "/usr/local/bin/phasher")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
IN_DIR = os.environ.get("HASHER_IN_DIR", "/downloads").rstrip("/")
LISTEN_HOST = os.environ.get("HASHER_HTTP_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("HASHER_HTTP_PORT", "8089"))
AUTH_TOKEN = os.environ.get("HASHER_HTTP_TOKEN", "").strip()
BODY_MAX = int(os.environ.get("HASHER_HTTP_BODY_MAX", "8192"))
PHASH_TIMEOUT = int(os.environ.get("HASHER_PHASH_TIMEOUT_SEC", "1800"))
MD5_CHUNK = int(os.environ.get("HASHER_MD5_CHUNK_BYTES", str(4 * 1024 * 1024)))
VERBOSE = os.environ.get("HASHER_HTTP_VERBOSE", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# While phasher runs (no per-frame API from upstream), emit heartbeat events this often.
PHASHER_HEARTBEAT_SEC = float(os.environ.get("HASHER_PHASHER_HEARTBEAT_SEC", "1.0"))
# MD5 progress: emit at least every N bytes read (plus on completion).
MD5_PROGRESS_BYTES = max(
    1,
    int(os.environ.get("HASHER_MD5_PROGRESS_BYTES", str(8 * 1024 * 1024))),
)

OSHASH_CHUNK = 65536  # Stash uses 64KB head + 64KB tail (OpenSubtitles hash)
PHASH_HEX_RE = re.compile(r"^[0-9a-fA-F]{16}$")
PHASH_PROG_RE = re.compile(r"^STASH_PHASH_PROGRESS\s+(\d+)\s+(\d+)\s*$")
ALGORITHMS = ("OSHASH", "MD5", "PHASH")

# PHASH and ffprobe are CPU-heavy; serialize them so a small VPS stays responsive.
# OSHASH/MD5 are file-I/O bound and run unlocked.
_phash_lock = threading.Lock()


def _phash_progress_from_stderr(stderr_lines: deque[str], elapsed_sec: float) -> dict[str, Any]:
    """Parse `STASH_PHASH_PROGRESS <n> <total>` lines emitted by patched Stash phasher."""
    extra: dict[str, Any] = {}
    for line in reversed(stderr_lines):
        m = PHASH_PROG_RE.search((line or "").strip())
        if not m:
            continue
        cur, tot = int(m.group(1)), int(m.group(2))
        if tot <= 0:
            break
        extra["phash_frame"] = cur
        extra["phash_frames_total"] = tot
        extra["pct"] = round(100.0 * cur / tot, 2)
        if cur > 0 and elapsed_sec > 0:
            extra["eta_sec"] = max(0.0, round((tot - cur) * (elapsed_sec / cur), 1))
        break
    return extra


def _json_response(handler: BaseHTTPRequestHandler, status: int, obj: dict[str, Any]) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _check_auth(handler: BaseHTTPRequestHandler) -> bool:
    if not AUTH_TOKEN:
        return True
    auth = handler.headers.get("Authorization", "")
    m = re.match(r"^\s*Bearer\s+(\S+)\s*$", auth, re.I)
    return bool(m and m.group(1) == AUTH_TOKEN)


def _safe_filename(raw: Any) -> str:
    if not raw or not isinstance(raw, str):
        raise ValueError("filename must be a non-empty string")
    if "\x00" in raw:
        raise ValueError("invalid filename")
    if "/" in raw or "\\" in raw or raw != os.path.basename(raw):
        raise ValueError("only a bare filename is allowed (no path components)")
    if raw in (".", "..") or ".." in raw:
        raise ValueError("invalid filename")
    return raw


def _resolve_path(filename: str) -> str:
    return os.path.join(IN_DIR, filename)


def _ffprobe_duration(path: str) -> float | None:
    try:
        cp = subprocess.run(
            [
                FFPROBE_BIN,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    raw = (cp.stdout or "").strip().splitlines()[:1]
    if not raw:
        return None
    try:
        return float(raw[0])
    except ValueError:
        return None


def _ffprobe_video_dimensions(path: str) -> tuple[int | None, int | None]:
    """First video stream width × height (same probe thumber uses)."""
    try:
        cp = subprocess.run(
            [
                FFPROBE_BIN,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    if cp.returncode != 0:
        return None, None
    lines = [ln.strip() for ln in (cp.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None, None
    try:
        return int(lines[0]), int(lines[1])
    except ValueError:
        return None, None


def _compute_oshash(path: str, emit=None) -> str:
    """Stash-compatible OpenSubtitles hash: file_size + sum of 64-bit LE chunks
    over the first 64KB and last 64KB, modulo 2**64."""
    size = os.path.getsize(path)
    if size < OSHASH_CHUNK:
        raise ValueError(f"file too small for oshash ({size} < {OSHASH_CHUNK} bytes)")
    if emit:
        emit(
            {
                "type": "phase",
                "algorithm": "OSHASH",
                "phase": "read_head",
                "total_bytes": size,
            }
        )
    h = size
    with open(path, "rb") as f:
        head = f.read(OSHASH_CHUNK)
        if emit:
            emit(
                {
                    "type": "phase",
                    "algorithm": "OSHASH",
                    "phase": "read_tail",
                    "total_bytes": size,
                }
            )
        f.seek(-OSHASH_CHUNK, os.SEEK_END)
        tail = f.read(OSHASH_CHUNK)
    if len(head) != OSHASH_CHUNK or len(tail) != OSHASH_CHUNK:
        raise ValueError("could not read full head/tail chunks for oshash")
    chunks = OSHASH_CHUNK // 8
    fmt = "<" + "Q" * chunks
    head_vals = struct.unpack_from(fmt, head, 0)
    tail_vals = struct.unpack_from(fmt, tail, 0)
    for v in head_vals:
        h += v
    for v in tail_vals:
        h += v
    h &= (1 << 64) - 1
    if emit:
        emit({"type": "phase", "algorithm": "OSHASH", "phase": "finalize"})
    return f"{h:016x}"


def _compute_md5(path: str, emit=None) -> str:
    size = os.path.getsize(path)
    h = hashlib.md5()
    read = 0
    next_emit_at = 0
    with open(path, "rb") as f:
        while True:
            buf = f.read(MD5_CHUNK)
            if not buf:
                break
            h.update(buf)
            read += len(buf)
            if emit and read >= next_emit_at:
                pct = round((read / size) * 100.0, 2) if size > 0 else None
                emit(
                    {
                        "type": "progress",
                        "algorithm": "MD5",
                        "read_bytes": read,
                        "total_bytes": size,
                        "pct": pct,
                    }
                )
                next_emit_at = read + MD5_PROGRESS_BYTES
    if emit:
        emit(
            {
                "type": "progress",
                "algorithm": "MD5",
                "read_bytes": read,
                "total_bytes": size,
                "pct": 100.0 if size > 0 else None,
                "phase": "complete",
            }
        )
    return h.hexdigest()


def _compute_phash(path: str, emit=None) -> tuple[str | None, int, str]:
    """Return (hex_hash_or_none, exit_code, combined_logs).

    When emit is set, run phasher with Popen and emit heartbeat + stderr tail
    (ffmpeg sometimes writes progress to stderr; phasher itself is quiet with -q).
    """
    if emit is None:
        cp = subprocess.run(
            [PHASHER_BIN, "-q", path],
            capture_output=True,
            text=True,
            timeout=PHASH_TIMEOUT,
        )
        out = cp.stdout or ""
        err = cp.stderr or ""
        logs = out + (("\n" + err) if err else "")
        if cp.returncode != 0:
            return None, cp.returncode, logs
        for line in out.splitlines():
            token = line.strip().split()[0] if line.strip() else ""
            if token and PHASH_HEX_RE.match(token):
                return token.lower(), 0, logs
        return None, cp.returncode, logs

    emit({"type": "phase", "algorithm": "PHASH", "phase": "phasher_start"})
    proc = subprocess.Popen(
        [PHASHER_BIN, "-q", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
    )
    stderr_lines: deque[str] = deque(maxlen=80)
    emit_lock = threading.Lock()
    t0 = time.monotonic()

    def _emit_phash_heartbeat(extra: dict[str, Any] | None = None) -> None:
        if not emit:
            return
        elapsed = round(time.monotonic() - t0, 1)
        tail = "\n".join(stderr_lines)[-600:] if stderr_lines else ""
        payload: dict[str, Any] = {
            "type": "heartbeat",
            "algorithm": "PHASH",
            "phase": "phasher_running",
            "elapsed_sec": elapsed,
            "stderr_tail": tail,
            **_phash_progress_from_stderr(stderr_lines, float(elapsed)),
        }
        if extra:
            payload.update(extra)
        with emit_lock:
            emit(payload)

    def _pump_stderr() -> None:
        try:
            if proc.stderr is None:
                return
            for line in iter(proc.stderr.readline, ""):
                line = line.rstrip()
                stderr_lines.append(line)
                if PHASH_PROG_RE.search(line):
                    _emit_phash_heartbeat()
        except Exception:
            pass

    t_err = threading.Thread(target=_pump_stderr, daemon=True)
    t_err.start()
    deadline = t0 + float(PHASH_TIMEOUT)
    try:
        while proc.poll() is None:
            if time.monotonic() > deadline:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                _emit_phash_heartbeat({"timeout": True})
                tail = "\n".join(stderr_lines)[-600:] if stderr_lines else ""
                return None, -1, tail
            _emit_phash_heartbeat()
            time.sleep(max(0.05, PHASHER_HEARTBEAT_SEC))
        out, err = proc.communicate(timeout=30)
    except Exception as e:  # noqa: BLE001
        try:
            proc.kill()
        except OSError:
            pass
        return None, -1, str(e)

    out = out or ""
    err_part = err or ""
    logs = out + (("\n" + err_part) if err_part else "")
    if proc.returncode != 0:
        return None, proc.returncode or -1, logs
    for line in out.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if token and PHASH_HEX_RE.match(token):
            return token.lower(), 0, logs
    return None, proc.returncode or -1, logs


def _hash_for(filename: str, algorithm: str) -> dict[str, Any]:
    """Compute a single fingerprint and return a JSON-ready response payload."""
    full_path = _resolve_path(filename)
    if not os.path.isfile(full_path):
        return {
            "ok": False,
            "error": "file_not_found",
            "filename": filename,
            "algorithm": algorithm,
            "search_dir": IN_DIR,
        }

    duration = _ffprobe_duration(full_path)
    width, height = _ffprobe_video_dimensions(full_path)

    if algorithm == "OSHASH":
        try:
            h = _compute_oshash(full_path)
        except (OSError, ValueError) as e:
            return {
                "ok": False,
                "error": "oshash_failed",
                "detail": str(e),
                "filename": filename,
                "algorithm": algorithm,
            }
        return {
            "ok": True,
            "filename": filename,
            "algorithm": "OSHASH",
            "hash": h,
            "duration": duration,
            "duration_int": int(duration) if duration is not None else None,
            "width": width,
            "height": height,
        }

    if algorithm == "MD5":
        try:
            h = _compute_md5(full_path)
        except OSError as e:
            return {
                "ok": False,
                "error": "md5_failed",
                "detail": str(e),
                "filename": filename,
                "algorithm": algorithm,
            }
        return {
            "ok": True,
            "filename": filename,
            "algorithm": "MD5",
            "hash": h,
            "duration": duration,
            "duration_int": int(duration) if duration is not None else None,
            "width": width,
            "height": height,
        }

    if algorithm == "PHASH":
        if not _phash_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": "busy",
                "filename": filename,
                "algorithm": algorithm,
            }
        try:
            try:
                h, rc, logs = _compute_phash(full_path)
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "error": "phash_timeout",
                    "filename": filename,
                    "algorithm": algorithm,
                    "timeout_sec": PHASH_TIMEOUT,
                }
            except FileNotFoundError as e:
                return {
                    "ok": False,
                    "error": "phasher_missing",
                    "detail": str(e),
                    "filename": filename,
                    "algorithm": algorithm,
                }
            if h is None:
                return {
                    "ok": False,
                    "error": "phash_failed",
                    "exit_code": rc,
                    "logs": logs,
                    "filename": filename,
                    "algorithm": algorithm,
                }
            return {
                "ok": True,
                "filename": filename,
                "algorithm": "PHASH",
                "hash": h,
                "duration": duration,
                "duration_int": int(duration) if duration is not None else None,
                "width": width,
                "height": height,
                "exit_code": rc,
                "logs": logs,
            }
        finally:
            _phash_lock.release()

    return {
        "ok": False,
        "error": "unknown_algorithm",
        "filename": filename,
        "algorithm": algorithm,
        "supported": list(ALGORITHMS),
    }


def _begin_chunked_ndjson(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()


def _emit_chunked_obj(handler: BaseHTTPRequestHandler, obj: dict[str, Any]) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
    handler.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
    handler.wfile.write(data)
    handler.wfile.write(b"\r\n")
    handler.wfile.flush()


def _end_chunked(handler: BaseHTTPRequestHandler) -> None:
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


def _stream_hash(handler: BaseHTTPRequestHandler, filename: str, algorithm: str) -> None:
    """Chunked NDJSON: progress/phase/heartbeat lines, then one {\"type\":\"result\",...} line."""
    _begin_chunked_ndjson(handler)
    emit = lambda o: _emit_chunked_obj(handler, o)  # noqa: E731
    try:
        full_path = _resolve_path(filename)
        if not os.path.isfile(full_path):
            _emit_chunked_obj(
                handler,
                {
                    "type": "result",
                    "ok": False,
                    "error": "file_not_found",
                    "filename": filename,
                    "algorithm": algorithm,
                    "search_dir": IN_DIR,
                },
            )
            return

        emit({"type": "phase", "phase": "ffprobe", "algorithm": algorithm})
        duration = _ffprobe_duration(full_path)
        width, height = _ffprobe_video_dimensions(full_path)
        emit(
            {
                "type": "phase",
                "phase": "ffprobe_done",
                "algorithm": algorithm,
                "duration": duration,
            }
        )

        if algorithm == "OSHASH":
            try:
                h = _compute_oshash(full_path, emit=emit)
            except (OSError, ValueError) as e:
                _emit_chunked_obj(
                    handler,
                    {
                        "type": "result",
                        "ok": False,
                        "error": "oshash_failed",
                        "detail": str(e),
                        "filename": filename,
                        "algorithm": algorithm,
                    },
                )
                return
            _emit_chunked_obj(
                handler,
                {
                    "type": "result",
                    "ok": True,
                    "filename": filename,
                    "algorithm": "OSHASH",
                    "hash": h,
                    "duration": duration,
                    "duration_int": int(duration) if duration is not None else None,
                    "width": width,
                    "height": height,
                },
            )
            return

        if algorithm == "MD5":
            try:
                h = _compute_md5(full_path, emit=emit)
            except OSError as e:
                _emit_chunked_obj(
                    handler,
                    {
                        "type": "result",
                        "ok": False,
                        "error": "md5_failed",
                        "detail": str(e),
                        "filename": filename,
                        "algorithm": algorithm,
                    },
                )
                return
            _emit_chunked_obj(
                handler,
                {
                    "type": "result",
                    "ok": True,
                    "filename": filename,
                    "algorithm": "MD5",
                    "hash": h,
                    "duration": duration,
                    "duration_int": int(duration) if duration is not None else None,
                    "width": width,
                    "height": height,
                },
            )
            return

        if algorithm == "PHASH":
            emit({"type": "phase", "phase": "waiting_for_phash_slot", "algorithm": "PHASH"})
            if not _phash_lock.acquire(blocking=False):
                _emit_chunked_obj(
                    handler,
                    {
                        "type": "result",
                        "ok": False,
                        "error": "busy",
                        "filename": filename,
                        "algorithm": algorithm,
                    },
                )
                return
            try:
                emit({"type": "phase", "phase": "phash_slot_acquired", "algorithm": "PHASH"})
                try:
                    h, rc, logs = _compute_phash(full_path, emit=emit)
                except subprocess.TimeoutExpired:
                    _emit_chunked_obj(
                        handler,
                        {
                            "type": "result",
                            "ok": False,
                            "error": "phash_timeout",
                            "filename": filename,
                            "algorithm": algorithm,
                            "timeout_sec": PHASH_TIMEOUT,
                        },
                    )
                    return
                except FileNotFoundError as e:
                    _emit_chunked_obj(
                        handler,
                        {
                            "type": "result",
                            "ok": False,
                            "error": "phasher_missing",
                            "detail": str(e),
                            "filename": filename,
                            "algorithm": algorithm,
                        },
                    )
                    return
                if h is None:
                    _emit_chunked_obj(
                        handler,
                        {
                            "type": "result",
                            "ok": False,
                            "error": "phash_failed",
                            "exit_code": rc,
                            "logs": logs,
                            "filename": filename,
                            "algorithm": algorithm,
                        },
                    )
                    return
                _emit_chunked_obj(
                    handler,
                    {
                        "type": "result",
                        "ok": True,
                        "filename": filename,
                        "algorithm": "PHASH",
                        "hash": h,
                        "duration": duration,
                        "duration_int": int(duration) if duration is not None else None,
                        "width": width,
                        "height": height,
                        "exit_code": rc,
                        "logs": logs,
                    },
                )
            finally:
                _phash_lock.release()
            return

        _emit_chunked_obj(
            handler,
            {
                "type": "result",
                "ok": False,
                "error": "unknown_algorithm",
                "filename": filename,
                "algorithm": algorithm,
                "supported": list(ALGORITHMS),
            },
        )
    except Exception as e:  # noqa: BLE001
        _emit_chunked_obj(
            handler,
            {
                "type": "result",
                "ok": False,
                "error": "stream_failed",
                "detail": str(e),
                "filename": filename,
                "algorithm": algorithm,
            },
        )
    finally:
        _end_chunked(handler)


_PATH_TO_ALGO = {
    "/v1/hash": None,    # algorithm comes from body
    "/v1/oshash": "OSHASH",
    "/v1/md5": "MD5",
    "/v1/phash": "PHASH",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            "[%s] %s - %s" % (self.log_date_time_string(), self.address_string(), fmt % args),
            flush=True,
        )

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/v1/health"):
            _json_response(self, 200, {"status": "ok", "service": "hasher-http"})
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not _check_auth(self):
            _json_response(self, 401, {"error": "unauthorized"})
            return

        # Match thumber-http: answer Expect: 100-continue before reading the body
        # so urllib3-based clients do not reset the connection.
        if self.headers.get("Expect", "").strip().lower() == "100-continue":
            self.send_response_only(HTTPStatus.CONTINUE)
            self.end_headers()

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path not in _PATH_TO_ALGO and path != "/v1/hash_stream":
            _json_response(self, 404, {"error": "not_found"})
            return

        length_hdr = self.headers.get("Content-Length")
        try:
            length = int(length_hdr) if length_hdr else 0
        except ValueError:
            _json_response(self, 400, {"error": "bad_content_length"})
            return
        if length <= 0 or length > BODY_MAX:
            _json_response(self, 400, {"error": "body_too_large_or_missing"})
            return

        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _json_response(self, 400, {"error": "invalid_json"})
            return

        try:
            filename = _safe_filename(body.get("filename"))
        except ValueError as e:
            _json_response(self, 400, {"error": "bad_filename", "detail": str(e)})
            return

        path_algo = _PATH_TO_ALGO.get(path)
        if path_algo is not None:
            algorithm = path_algo
        else:
            algorithm = (body.get("algorithm") or "PHASH").strip().upper()
        if algorithm not in ALGORITHMS:
            _json_response(
                self, 400,
                {"error": "unknown_algorithm", "supported": list(ALGORITHMS)},
            )
            return

        if VERBOSE:
            print(
                f"[hasher-http] {algorithm} {filename!r}",
                flush=True,
            )

        if path == "/v1/hash_stream":
            _stream_hash(self, filename, algorithm)
            return

        result = _hash_for(filename, algorithm)
        ok = bool(result.get("ok"))
        status = 200 if ok else (404 if result.get("error") == "file_not_found" else
                                 503 if result.get("error") == "busy" else 500)
        _json_response(self, status, result)


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(
        f"hasher-http listening on http://{LISTEN_HOST}:{LISTEN_PORT}  "
        f"in_dir={IN_DIR}  algos={','.join(ALGORITHMS)}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
