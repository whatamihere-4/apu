#!/usr/bin/env python3
"""
HTTP API for thumber: POST a basename; videos live under THUMBER_IN_DIR (e.g. /downloads).
Stdlib only. Run via docker-compose service thumber-http.
"""
from __future__ import annotations

import json
import os
import re
import select
from http import HTTPStatus
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

THUMBS_SCRIPT = os.environ.get("THUMBER_THUMBS_SCRIPT", "/usr/local/bin/thumbs")
LISTEN_HOST = os.environ.get("THUMBER_HTTP_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("THUMBER_HTTP_PORT", "8080"))
AUTH_TOKEN = os.environ.get("THUMBER_HTTP_TOKEN", "").strip()
BODY_MAX = int(os.environ.get("THUMBER_HTTP_BODY_MAX", "8192"))
SUBPROCESS_TIMEOUT = int(os.environ.get("THUMBER_HTTP_TIMEOUT_SEC", "7200"))
VERBOSE = os.environ.get("THUMBER_HTTP_VERBOSE", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

_run_lock = threading.Lock()


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


def _safe_filename(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        raise ValueError("filename must be a non-empty string")
    if "\x00" in raw:
        raise ValueError("invalid filename")
    if "/" in raw or "\\" in raw or raw != os.path.basename(raw):
        raise ValueError("only a bare filename is allowed (no path components)")
    if raw in (".", "..") or ".." in raw:
        raise ValueError("invalid filename")
    return raw


def _expected_output_path(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    out_dir = os.environ.get("THUMBER_OUT_DIR", "").rstrip("/")
    if out_dir:
        return f"{out_dir}/{stem}_thumbs.png"
    return f"{stem}_thumbs.png"


def _run_thumbs(filename: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", THUMBS_SCRIPT, filename],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        env=os.environ.copy(),
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s - %s" % (self.log_date_time_string(), self.address_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/v1/health"):
            _json_response(self, 200, {"status": "ok", "service": "thumber-http"})
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not _check_auth(self):
            _json_response(self, 401, {"error": "unauthorized"})
            return

        # urllib3/requests may use Expect: 100-continue on POST; answer before reading the body
        # so the client does not reset the connection (RemoteDisconnected).
        if self.headers.get("Expect", "").strip().lower() == "100-continue":
            self.send_response_only(HTTPStatus.CONTINUE)
            self.end_headers()

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path not in ("/v1/thumbs", "/v1/thumbs/stream"):
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

        filename = body.get("filename")
        try:
            safe = _safe_filename(filename)
        except ValueError as e:
            _json_response(self, 400, {"error": "bad_filename", "detail": str(e)})
            return

        if path == "/v1/thumbs/stream":
            self._handle_stream(safe)
            return

        with _run_lock:
            cp = _run_thumbs(safe)

        logs = (cp.stdout or "") + (cp.stderr or "")
        out_path = _expected_output_path(safe)
        ok = cp.returncode == 0
        payload: dict[str, Any] = {
            "ok": ok,
            "filename": safe,
            "exit_code": cp.returncode,
            "output_path": out_path,
            "logs": logs,
        }
        if not ok:
            payload["error"] = "thumbs_failed"
        _json_response(self, 200 if ok else 500, payload)

    def _handle_stream(self, safe: str) -> None:
        stats: dict[str, int] = {"sse_bytes": 0, "log_lines": 0}

        def write_sse(obj: dict[str, Any]) -> None:
            line = json.dumps(obj, ensure_ascii=False)
            raw = f"data: {line}\n\n".encode("utf-8")
            self.wfile.write(raw)
            self.wfile.flush()
            stats["sse_bytes"] += len(raw)
            if obj.get("type") == "log":
                stats["log_lines"] += 1

        if not _run_lock.acquire(blocking=False):
            _json_response(self, 503, {"error": "busy"})
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # Merge bash+ffmpeg into one pipe. ffmpeg can write lots of stderr without newlines;
            # that fills the pipe and deadlocks if we only read line-by-line. thumbs.sh sends ffmpeg
            # stderr to /dev/null; we also read in chunks so a long line cannot block the pipe.
            proc = subprocess.Popen(
                ["/bin/bash", THUMBS_SCRIPT, safe],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=os.environ.copy(),
            )
            assert proc.stdout is not None

            stop_watchdog = threading.Event()

            def _watchdog() -> None:
                start = time.monotonic()
                while not stop_watchdog.wait(15.0):
                    if proc.poll() is not None:
                        return
                    elapsed = time.monotonic() - start
                    print(
                        "[thumber-http] thumbs.sh still running "
                        f"file={safe!r} pid={proc.pid} elapsed_s={elapsed:.1f} "
                        f"sse_log_lines={stats['log_lines']} sse_bytes={stats['sse_bytes']}",
                        flush=True,
                    )

            if VERBOSE:
                print(
                    f"[thumber-http] stream start file={safe!r} pid={proc.pid} verbose=1",
                    flush=True,
                )
                threading.Thread(
                    target=_watchdog, name="thumber-watchdog", daemon=True
                ).start()

            started = time.monotonic()
            buf = b""
            timed_out = False
            rc = None
            try:
                while True:
                    if time.monotonic() - started > SUBPROCESS_TIMEOUT:
                        timed_out = True
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        rc = 124
                        write_sse(
                            {
                                "type": "error",
                                "detail": f"thumbs timeout after {SUBPROCESS_TIMEOUT}s",
                            }
                        )
                        break
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if not ready:
                        if proc.poll() is not None:
                            break
                        continue
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        if proc.poll() is not None:
                            break
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        text = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        write_sse({"type": "log", "line": text})
                if buf:
                    text = buf.decode("utf-8", errors="replace").rstrip("\r")
                    write_sse({"type": "log", "line": text})
            finally:
                stop_watchdog.set()
                proc.stdout.close()
            if rc is None:
                rc = proc.wait(timeout=5)

            if VERBOSE:
                print(
                    "[thumber-http] stream end "
                    f"file={safe!r} exit_code={rc} "
                    f"sse_log_lines={stats['log_lines']} sse_bytes={stats['sse_bytes']} "
                    f"timed_out={int(timed_out)}",
                    flush=True,
                )

            write_sse(
                {
                    "type": "done",
                    "ok": rc == 0,
                    "exit_code": rc,
                    "filename": safe,
                    "output_path": _expected_output_path(safe),
                }
            )
        except Exception as e:  # noqa: BLE001
            try:
                write_sse({"type": "error", "detail": str(e)})
            except OSError:
                pass
        finally:
            _run_lock.release()


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"thumber-http listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
