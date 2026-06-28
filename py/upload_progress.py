"""Job upload progress reporting for the main-page UI (single + split Filester)."""
from __future__ import annotations


class UploadProgressReporter:
    """Callable progress hook with optional split-upload helpers.

    GoFile / single-file uploads use ``__call__`` only. Filester split uploads
    call ``set_splitting``, ``register_part``, ``wrap_part``, ``complete_part``.
    """

    def __init__(
        self,
        jobs: dict,
        job_id: str,
        folder_name: str,
        *,
        format_size,
        is_cancelled,
    ):
        self._jobs = jobs
        self._job_id = job_id
        self._folder_name = folder_name or "Root"
        self._format_size = format_size
        self._is_cancelled = is_cancelled
        self._split_mode = False
        self._source_bytes = 0
        self._part_count = 0
        self._parts: dict[int, dict] = {}
        self._current_part: int | None = None

    def _job(self):
        return self._jobs.get(self._job_id)

    def _dest(self) -> str:
        return f" → {self._folder_name}" if self._folder_name != "Root" else ""

    def _commit(self, progress: dict, status_text: str) -> None:
        job = self._job()
        if not job:
            return
        job["progress"] = progress
        job["status_text"] = status_text

    def set_splitting(self, *, source_bytes: int = 0, label: str = "") -> None:
        self._split_mode = True
        self._source_bytes = int(source_bytes or 0)
        msg = label or "Splitting video for Filester upload…"
        self._commit(
            {
                "type": "upload",
                "phase": "splitting",
                "mode": "split",
                "percent": 0,
                "source_bytes": self._source_bytes,
                "source_fmt": self._format_size(self._source_bytes) if self._source_bytes else "",
                "label": msg,
                "folder_name": self._folder_name,
                "parts": [],
            },
            f"{msg}{self._dest()}",
        )

    def register_part(self, part_index: int, label: str, size_bytes: int, part_count: int) -> None:
        idx = int(part_index or 0)
        if idx <= 0:
            return
        self._split_mode = True
        self._part_count = max(self._part_count, int(part_count or 0))
        if idx not in self._parts:
            self._parts[idx] = {
                "index": idx,
                "label": label,
                "size_bytes": int(size_bytes or 0),
                "percent": 0.0,
                "status": "pending",
            }
        else:
            rec = self._parts[idx]
            rec["label"] = label or rec["label"]
            if size_bytes:
                rec["size_bytes"] = int(size_bytes)
        self._publish_uploading()

    def wrap_part(self, part_index: int):
        idx = int(part_index or 0)

        def cb(pct, uploaded, total, speed, eta):
            self.part_progress(idx, pct, uploaded, total, speed, eta)

        return cb

    def complete_part(self, part_index: int) -> None:
        idx = int(part_index or 0)
        rec = self._parts.get(idx)
        if rec:
            rec["percent"] = 100.0
            rec["status"] = "done"
        if self._current_part == idx:
            self._current_part = None
        self._publish_uploading()

    def part_progress(
        self,
        part_index: int,
        pct,
        uploaded,
        total,
        speed,
        eta,
    ) -> None:
        if self._is_cancelled():
            return
        idx = int(part_index or 0)
        rec = self._parts.get(idx)
        if not rec:
            return
        self._current_part = idx
        rec["percent"] = round(float(pct), 1)
        rec["status"] = "done" if pct >= 99.95 else "uploading"
        overall = self._overall_bytes()
        total_all = self._total_bytes()
        overall_pct = (overall / total_all * 100.0) if total_all > 0 else float(pct)
        parts_list = self._parts_payload()
        part_label = rec["label"]
        n = self._part_count or len(self._parts)
        status = (
            f"Uploading part {idx}/{n}: {part_label} — {pct:.1f}% "
            f"({self._format_size(uploaded)}/{self._format_size(total)}) "
            f"@ {self._format_size(speed)}/s — overall {overall_pct:.1f}%"
        )
        if int(eta) > 0:
            status += f" — ETA {int(eta)}s"
        status += self._dest()
        self._commit(
            {
                "type": "upload",
                "phase": "uploading",
                "mode": "split",
                "percent": round(overall_pct, 1),
                "uploaded": overall,
                "total": total_all,
                "speed": speed,
                "eta": int(eta),
                "uploaded_fmt": self._format_size(overall),
                "total_fmt": self._format_size(total_all),
                "speed_fmt": f"{self._format_size(speed)}/s",
                "folder_name": self._folder_name,
                "part_index": idx,
                "part_count": n,
                "current_part": idx,
                "parts": parts_list,
            },
            status,
        )

    def _total_bytes(self) -> int:
        if self._source_bytes > 0:
            return self._source_bytes
        return sum(int(p.get("size_bytes") or 0) for p in self._parts.values())

    def _overall_bytes(self) -> int:
        total = 0.0
        for p in self._parts.values():
            size = int(p.get("size_bytes") or 0)
            pct = float(p.get("percent") or 0)
            status = p.get("status")
            if status == "done":
                total += size
            elif status == "uploading":
                total += size * (pct / 100.0)
        return int(total)

    def _parts_payload(self) -> list[dict]:
        out = []
        for idx in sorted(self._parts):
            p = self._parts[idx]
            out.append({
                "index": p["index"],
                "label": p["label"],
                "percent": p["percent"],
                "status": p["status"],
                "size_fmt": self._format_size(p.get("size_bytes") or 0),
            })
        if self._part_count > len(out):
            for idx in range(len(out) + 1, self._part_count + 1):
                out.append({
                    "index": idx,
                    "label": f"Part {idx}",
                    "percent": 0.0,
                    "status": "pending",
                    "size_fmt": "",
                })
        return out

    def _publish_uploading(self) -> None:
        overall = self._overall_bytes()
        total_all = self._total_bytes()
        overall_pct = (overall / total_all * 100.0) if total_all > 0 else 0.0
        n = self._part_count or len(self._parts)
        cur = self._current_part or 0
        status = f"Uploading split parts ({n} total) — overall {overall_pct:.1f}%{self._dest()}"
        if cur:
            status = f"Uploading part {cur}/{n} — overall {overall_pct:.1f}%{self._dest()}"
        self._commit(
            {
                "type": "upload",
                "phase": "uploading",
                "mode": "split",
                "percent": round(overall_pct, 1),
                "uploaded": overall,
                "total": total_all,
                "uploaded_fmt": self._format_size(overall),
                "total_fmt": self._format_size(total_all),
                "speed_fmt": "",
                "folder_name": self._folder_name,
                "part_count": n,
                "current_part": cur or None,
                "parts": self._parts_payload(),
            },
            status,
        )

    def __call__(self, pct, uploaded, total, speed, eta) -> None:
        if self._is_cancelled():
            return
        dest = self._dest()
        self._commit(
            {
                "type": "upload",
                "phase": "uploading",
                "mode": "single",
                "percent": round(float(pct), 1),
                "uploaded": uploaded,
                "total": total,
                "speed": speed,
                "eta": int(eta),
                "uploaded_fmt": self._format_size(uploaded),
                "total_fmt": self._format_size(total),
                "speed_fmt": f"{self._format_size(speed)}/s",
                "folder_name": self._folder_name,
            },
            (
                f"Uploading{dest}: {pct:.1f}% — "
                f"{self._format_size(uploaded)}/{self._format_size(total)} "
                f"@ {self._format_size(speed)}/s — ETA {int(eta)}s"
            ),
        )
