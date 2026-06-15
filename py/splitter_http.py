#!/usr/bin/env python3
"""HTTP API for the splitter sidecar.

POST /v1/split with JSON {"path": "/downloads/movie.mkv", "max_bytes": 9500000000,
"output_dir": "/downloads/.split/<id>"} -> {"parts": ["/downloads/.split/<id>/movie.PART1.mkv", ...]}.

One split runs at a time (returns 503 busy if locked). Stdlib only; ffmpeg
stream-copy logic lives in file_splitter.py. Run via compose service splitter-http.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import file_splitter

LISTEN_HOST = os.environ.get("SPLITTER_HTTP_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("SPLITTER_HTTP_PORT", "8090"))
AUTH_TOKEN = os.environ.get("SPLITTER_HTTP_TOKEN", "").strip()
BODY_MAX = int(os.environ.get("SPLITTER_HTTP_BODY_MAX", "65536"))
FFMPEG_TIMEOUT = int(os.environ.get("SPLITTER_FFMPEG_TIMEOUT_SEC", "7200"))
IN_DIR = os.path.realpath(os.environ.get("SPLITTER_IN_DIR", "/downloads"))

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


def _under_in_dir(path: str) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return real == IN_DIR or real.startswith(IN_DIR + os.sep)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 - keep stdout clean/structured
        print(f"[SPLITTER] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self):  # noqa: N802
        if self.path.split("?", 1)[0] == "/health":
            _json_response(self, HTTPStatus.OK, {"status": "healthy", "service": "splitter"})
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/v1/split":
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not _check_auth(self):
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > BODY_MAX:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid body length"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return

        path = (body.get("path") or "").strip()
        output_dir = (body.get("output_dir") or "").strip()
        try:
            max_bytes = int(body.get("max_bytes"))
        except (TypeError, ValueError):
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "max_bytes required"})
            return
        if not path or not output_dir:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "path and output_dir required"})
            return
        if not _under_in_dir(path) or not _under_in_dir(output_dir):
            _json_response(self, HTTPStatus.FORBIDDEN, {"error": "paths must be under the shared mount"})
            return
        if not os.path.isfile(path):
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": f"file not found: {path}"})
            return

        if not _run_lock.acquire(blocking=False):
            _json_response(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": "splitter busy"})
            return
        try:
            print(f"[SPLITTER] splitting {path} (max_bytes={max_bytes})", flush=True)
            parts = file_splitter.split_file(
                path,
                max_bytes,
                output_dir,
                on_log=lambda ln: print(f"[SPLITTER] {ln}", flush=True),
                ffmpeg_timeout=FFMPEG_TIMEOUT,
            )
            _json_response(self, HTTPStatus.OK, {"parts": parts, "count": len(parts)})
        except file_splitter.SplitError as e:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - report any failure to caller
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(e).__name__}: {e}"})
        finally:
            _run_lock.release()


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[SPLITTER] listening on {LISTEN_HOST}:{LISTEN_PORT} (in_dir={IN_DIR})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
