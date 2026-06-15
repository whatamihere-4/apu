"""Flask routes for queue export/import/restore (monolith).

Wire into app.py after _start_link_job / _start_path_job exist:

    from py.queue_api import register_queue_routes, restore_pending_queue

    restore = register_queue_routes(
        app,
        jobs=jobs,
        hashes_dir=HASHES_DIR,
        downloads_dir=DOWNLOADS_DIR,
        start_link_job=_start_link_job,
        start_path_job=_start_path_job,
    )
    # in if __name__ == "__main__": before app.run(...)
    if RESTORE_QUEUE:
        restore()
"""
from __future__ import annotations

import os

from flask import Response, jsonify, request

from queue_persist import (
    clear as _queue_clear,
    export_pending as _queue_export_pending,
    format_export_txt as _queue_format_export_txt,
    load_all as _queue_load_all,
)


def register_queue_routes(app, *, jobs, hashes_dir, downloads_dir, start_link_job, start_path_job):
    def restore_pending_queue():
        records = _queue_load_all(hashes_dir)
        if not records:
            return 0
        _queue_clear(hashes_dir)
        restored = 0
        for rec in records:
            kind = rec.get("type") or rec.get("job_kind")
            folder_id = (rec.get("folder_id") or "").strip() or None
            try:
                if kind == "link":
                    url = (rec.get("url") or "").strip()
                    if url:
                        start_link_job(url, folder_id)
                        restored += 1
                elif kind == "path":
                    path = (rec.get("path") or "").strip()
                    if (
                        path
                        and path.startswith(downloads_dir)
                        and os.path.exists(path)
                    ):
                        start_path_job(path, folder_id)
                        restored += 1
            except Exception as e:
                print(f"[QUEUE] Skipped restore for {rec!r}: {e}", flush=True)
        print(f"[QUEUE] Restored {restored} pending job(s) from disk", flush=True)
        return restored

    @app.route("/api/queue/export")
    def api_queue_export():
        payload = _queue_export_pending(hashes_dir, jobs)
        if request.args.get("format") == "txt":
            return Response(
                _queue_format_export_txt(payload),
                mimetype="text/plain; charset=utf-8",
            )
        return jsonify(payload)

    @app.route("/api/queue/import", methods=["POST"])
    def api_queue_import():
        data = request.get_json(silent=True) or {}
        items = data.get("items")
        if not isinstance(items, list):
            return jsonify({"error": "items array required"}), 400
        job_ids = []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            folder_id = (item.get("folder_id") or "").strip() or None
            if kind == "link":
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                job_ids.append(start_link_job(url, folder_id))
            elif kind == "path":
                path = (item.get("path") or "").strip()
                if not path:
                    continue
                if not path.startswith(downloads_dir) or not os.path.exists(path):
                    return jsonify({"error": f"Path not found: {path}"}), 404
                job_ids.append(start_path_job(path, folder_id))
        return jsonify({"job_ids": job_ids, "count": len(job_ids)})

    return restore_pending_queue
