"""Upload orchestrator: one or both of GoFile and Filester.

Enable providers independently via GOFILE_ENABLED / FILESTER_ENABLED, or the
legacy UPLOAD_PROVIDER shorthand (gofile, filester, dual). Filester oversized
uploads split via FILESTER_SPLIT_MODE: bytes, ffmpeg (one-pass), ffmpeg_slice
(per-part), or optimal (one-pass when disk allows, else FILESTER_SPLIT_FALLBACK).
"""
from __future__ import annotations

import os
import shutil
import uuid
from contextlib import nullcontext

import byte_splitter
import file_splitter
import filester_upload
import gofile_upload
import size_limits
from upload_common import UploadResult, format_size  # noqa: F401 (re-exported)
from upload_resume import (
    UploadResumeState,
    UploadedPart,
    cleanup_split_artifacts,
    load_upload_resume_state,
    resume_job_dir,
    save_upload_resume_state,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


DOWNLOADS_DIR = os.path.realpath(
    (os.environ.get("MEDIA_DOWNLOADS_DIR") or "./downloads").rstrip("/") or "./downloads"
)
FILESTER_MAX_PART_BYTES = _env_int("FILESTER_MAX_PART_BYTES", 10_200_547_328)
FILESTER_FFMPEG_TIMEOUT = max(300, _env_int("SPLITTER_FFMPEG_TIMEOUT_SEC", 1800))
FILESTER_FFPROBE_TIMEOUT = max(60, _env_int("SPLITTER_FFPROBE_TIMEOUT_SEC", 300))
SPLITTER_EXTRACT_BACKEND = (
    (os.environ.get("SPLITTER_EXTRACT_BACKEND") or "ffmpeg").strip().lower() or "ffmpeg"
)

_SPLIT_MODE_ALIASES = {
    "splice": "bytes",
    "byte": "bytes",
    "bytes": "bytes",
    "cat": "bytes",
    "ffmpeg": "ffmpeg",
    "ffmpeg_onepass": "ffmpeg",
    "onepass": "ffmpeg",
    "one_pass": "ffmpeg",
    "ffmpeg_slice": "ffmpeg_slice",
    "ffmpeg-slice": "ffmpeg_slice",
    "slice": "ffmpeg_slice",
    "optimal": "optimal",
    "auto": "optimal",
}


def _parse_split_mode(raw: str, *, default: str = "bytes") -> str:
    mode = _SPLIT_MODE_ALIASES.get(raw.strip().lower())
    if mode:
        return mode
    print(
        f"[UPLOAD] Unknown split mode {raw!r}; using {default}",
        flush=True,
    )
    return default


_raw_split_mode = (os.environ.get("FILESTER_SPLIT_MODE") or "bytes").strip().lower()
FILESTER_SPLIT_MODE = _parse_split_mode(_raw_split_mode)

_raw_fallback = (os.environ.get("FILESTER_SPLIT_FALLBACK") or "bytes").strip().lower()
FILESTER_SPLIT_FALLBACK = _parse_split_mode(_raw_fallback, default="bytes")
if FILESTER_SPLIT_FALLBACK not in ("bytes", "ffmpeg_slice"):
    print(
        f"[UPLOAD] FILESTER_SPLIT_FALLBACK must be bytes or ffmpeg_slice; "
        f"got {_raw_fallback!r}, using bytes",
        flush=True,
    )
    FILESTER_SPLIT_FALLBACK = "bytes"


def resolve_split_mode(file_size: int) -> str:
    """Pick the effective split mode for a file (resolves optimal)."""
    if FILESTER_SPLIT_MODE != "optimal":
        return FILESTER_SPLIT_MODE
    if file_size <= FILESTER_MAX_PART_BYTES:
        return "bytes"
    if size_limits.insufficient_disk_reason(
        file_size,
        FILESTER_MAX_PART_BYTES,
        download_dir=DOWNLOADS_DIR,
        split_mode="ffmpeg",
    ) is None:
        return "ffmpeg"
    return FILESTER_SPLIT_FALLBACK


def _split_mode_label(mode: str) -> str:
    return {
        "bytes": "byte-range splice (one part on disk at a time)",
        "ffmpeg": "ffmpeg one-pass stream-copy (playable parts, ~2× disk during split)",
        "ffmpeg_slice": "ffmpeg per-part stream-copy (playable parts, one part on disk at a time)",
    }.get(mode, mode)


class SplitUploadCoordinator:
    """Progressive Filester split upload: scene folder early, parts routed incrementally."""

    def __init__(
        self,
        *,
        studio_folder_id: str | None,
        fallback_name: str,
        get_split_meta=None,
        set_split_dest_folder_id=None,
        folder_lock=None,
        resume_state: UploadResumeState,
        resume_dir: str | None,
        on_log=None,
    ):
        self.studio_folder_id = (studio_folder_id or "").strip() or None
        self.fallback_name = fallback_name
        self._get_split_meta = get_split_meta
        self._set_split_dest = set_split_dest_folder_id
        self._folder_lock = folder_lock
        self._resume_state = resume_state
        self._resume_dir = resume_dir
        self._on_log = on_log
        self._dest_folder_id = (resume_state.split_dest_folder_id or "").strip() or None
        if self._dest_folder_id and set_split_dest_folder_id:
            set_split_dest_folder_id(self._dest_folder_id)

    @property
    def dest_folder_id(self) -> str | None:
        return self._dest_folder_id

    def _meta(self) -> dict:
        if not self._get_split_meta:
            return {}
        return self._get_split_meta() or {}

    def _save_dest(self, folder_id: str) -> None:
        fid = (folder_id or "").strip()
        if not fid:
            return
        self._dest_folder_id = fid
        self._resume_state.split_dest_folder_id = fid
        if self._resume_dir:
            save_upload_resume_state(self._resume_dir, self._resume_state)
        if self._set_split_dest:
            self._set_split_dest(fid)

    def sync_from_job(self) -> None:
        existing = (self._meta().get("dest_folder_id") or "").strip()
        if existing:
            self._dest_folder_id = existing
            self._resume_state.split_dest_folder_id = existing

    def ensure_scene_folder(self, *, allow_fallback: bool = False) -> str | None:
        """Create StashDB-titled folder (or filename fallback) under the studio folder."""
        lock = self._folder_lock() if self._folder_lock else nullcontext()
        with lock:
            self.sync_from_job()
            if self._dest_folder_id:
                dest = self._dest_folder_id
            elif not self.studio_folder_id:
                return None
            else:
                meta = self._meta()
                scene_title = (meta.get("scene_title") or "").strip()
                cover_path = meta.get("cover_path")
                folder_title = scene_title or (self.fallback_name if allow_fallback else None)
                if not folder_title:
                    return None
                label = (
                    f"split upload: {scene_title} (StashDB)"
                    if scene_title
                    else f"split upload: {self.fallback_name}"
                )
                try:
                    dest = filester_upload.prepare_split_scene_folder(
                        parent_folder_id=self.studio_folder_id,
                        folder_name=self.fallback_name,
                        folder_title=folder_title,
                        cover_image_path=cover_path if scene_title else None,
                        blacklist_label=label,
                        on_log=self._on_log,
                    )
                    self._save_dest(dest)
                except Exception as exc:  # noqa: BLE001
                    self.sync_from_job()
                    if self._dest_folder_id:
                        if self._on_log:
                            self._on_log(
                                "[Filester] Scene folder already created by parallel step; "
                                "using existing folder"
                            )
                        dest = self._dest_folder_id
                    else:
                        if self._on_log:
                            self._on_log(
                                f"[Filester] Scene folder create failed "
                                f"(upload continues in studio folder): {exc}"
                            )
                        return None
        if dest:
            cover = self._meta().get("cover_path")
            if cover:
                filester_upload.try_set_split_folder_thumbnail(
                    dest,
                    cover,
                    on_log=self._on_log,
                )
        return dest

    def upload_folder_for_part(self, part_index: int) -> str | None:
        self.sync_from_job()
        if part_index > 1 and self._dest_folder_id:
            return self._dest_folder_id
        return self.studio_folder_id

    def after_part_uploaded(self, part_index: int, raw: dict) -> None:
        if part_index != 1 or not self.studio_folder_id:
            return
        dest = self.ensure_scene_folder(allow_fallback=True)
        if not dest:
            self.sync_from_job()
            dest = self._dest_folder_id
        if not dest or dest == self.studio_folder_id:
            return
        filester_upload.move_upload_response_to_folder(raw, dest, on_log=self._on_log)


_legacy = (os.environ.get("UPLOAD_PROVIDER") or "").strip().lower()
if _legacy in ("dual", "both"):
    _default_gofile, _default_filester = True, True
elif _legacy == "filester":
    _default_gofile, _default_filester = False, True
else:
    _default_gofile, _default_filester = True, False

GOFILE_ENABLED = _env_bool("GOFILE_ENABLED", _default_gofile)
FILESTER_ENABLED = _env_bool("FILESTER_ENABLED", _default_filester)

if not GOFILE_ENABLED and not FILESTER_ENABLED:
    raise RuntimeError("At least one upload provider must be enabled (GOFILE_ENABLED / FILESTER_ENABLED)")

ACTIVE_PROVIDERS: list[str] = []
if GOFILE_ENABLED:
    ACTIVE_PROVIDERS.append("gofile")
if FILESTER_ENABLED:
    ACTIVE_PROVIDERS.append("filester")

_labels = []
if GOFILE_ENABLED:
    _labels.append("GoFile")
if FILESTER_ENABLED:
    _labels.append("Filester")
PROVIDER_LABEL = " + ".join(_labels)

UPLOAD_PROVIDER = "dual" if len(ACTIVE_PROVIDERS) > 1 else ACTIVE_PROVIDERS[0]

if GOFILE_ENABLED and not (os.environ.get("GOFILE_API_KEY") or "").strip():
    print("[UPLOAD] Warning: GOFILE_ENABLED but GOFILE_API_KEY is empty", flush=True)
if FILESTER_ENABLED and not (os.environ.get("FILESTER_API_KEY") or "").strip():
    print("[UPLOAD] Warning: FILESTER_ENABLED but FILESTER_API_KEY is empty", flush=True)


def _normalize_gofile(raw: dict) -> UploadResult:
    ok = raw.get("status") == "ok"
    url = (raw.get("data") or {}).get("downloadPage", "") if ok else ""
    return UploadResult(ok=ok, provider="gofile", gallery_url=url, raw=raw)


def _normalize_filester(raw: dict, *, part: dict | None = None) -> UploadResult:
    gallery_url = filester_upload.gallery_url_from_response(raw)
    ok = bool(raw.get("success")) and bool(gallery_url)
    part_count = 1
    part_index = 0
    original_basename = ""
    was_split = False
    split_mode = str(part.get("split_mode") or "") if part else ""
    if part:
        part_count = int(part.get("part_count") or 1)
        part_index = int(part.get("part_index") or 0)
        original_basename = str(part.get("original_basename") or "")
        was_split = part_count > 1
    return UploadResult(
        ok=ok,
        provider="filester",
        gallery_url=gallery_url,
        raw=raw,
        part_index=part_index,
        part_count=part_count,
        original_basename=original_basename,
        was_split=was_split,
        split_mode=split_mode,
    )


def get_root_folder_id():
    return gofile_upload.get_root_folder_id()


def create_folder(parent_id, name):
    return gofile_upload.create_folder(parent_id, name)


def folder_url(folder_id, *, provider: str = "gofile"):
    if provider == "filester":
        return filester_upload.folder_url(folder_id)
    return gofile_upload.folder_url(folder_id)


def plan_upload_destinations(file_size: int) -> tuple[list[str], str | None]:
    """Return (providers_to_use, filester_skip_reason).

    GoFile is included when enabled. Filester is skipped (not an error) when
    disk budget cannot accommodate splitting.
    """
    destinations: list[str] = []
    filester_skip: str | None = None

    if GOFILE_ENABLED:
        destinations.append("gofile")

    if FILESTER_ENABLED:
        mode = resolve_split_mode(file_size)
        skip = size_limits.oversize_skip_reason(
            file_size,
            FILESTER_MAX_PART_BYTES,
            download_dir=DOWNLOADS_DIR,
            split_mode=mode,
        )
        if skip:
            filester_skip = skip
        else:
            low = size_limits.insufficient_disk_reason(
                file_size,
                FILESTER_MAX_PART_BYTES,
                download_dir=DOWNLOADS_DIR,
                split_mode=mode,
            )
            if low:
                filester_skip = low
            else:
                destinations.append("filester")

    return destinations, filester_skip


def _expand_sources(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        out = []
        for root, _dirs, files in os.walk(path):
            for fname in sorted(files):
                out.append(os.path.join(root, fname))
        return out
    raise FileNotFoundError(f"Path not found: {path}")


def _upload_gofile(
    src: str,
    *,
    folder_id,
    on_progress,
    should_cancel,
) -> UploadResult:
    raw = gofile_upload.upload_file(
        src,
        folder_id=folder_id,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )
    return _normalize_gofile(raw)


def _upload_filester_file(
    filepath: str,
    *,
    folder_id,
    on_progress,
    should_cancel,
    should_restart=None,
    on_log=None,
) -> dict:
    from downloader import TransferCancelled, UploadRestartRequested

    while True:
        try:
            return filester_upload.upload_file(
                filepath,
                folder_id=folder_id,
                on_progress=on_progress,
                should_cancel=should_cancel,
                should_restart=should_restart,
                on_log=on_log,
            )
        except UploadRestartRequested:
            if on_log:
                on_log("[Filester] Watchdog: restarting this part upload...")
            continue
        except TransferCancelled:
            raise


def _result_from_resume_part(
    part: UploadedPart,
    src: str,
    split_mode: str,
    *,
    total_parts: int | None = None,
) -> UploadResult:
    part_count = int(total_parts or max(part.part_index, 1))
    meta = {
        "part_count": part_count,
        "part_index": part.part_index,
        "original_basename": os.path.basename(src),
        "split_mode": split_mode,
    }
    if part.upload_response:
        return _normalize_filester(part.upload_response, part=meta)
    return UploadResult(
        ok=bool(part.slug),
        provider="filester",
        gallery_url=filester_upload.gallery_url_from_response(part.upload_response)
        or (f"{filester_upload.FILESTER_SITE_URL}/d/{part.slug}" if part.slug else ""),
        raw=part.upload_response or {"slug": part.slug},
        part_index=part.part_index,
        part_count=meta["part_count"],
        original_basename=meta["original_basename"],
        was_split=part.part_index > 0 and meta["part_count"] > 1,
        split_mode=split_mode,
    )


def _upload_filester_parts(
    src: str,
    *,
    folder_id,
    on_progress,
    should_cancel,
    should_restart=None,
    on_log,
    job_id,
    delete_source: bool,
    resume_dir: str | None = None,
    resume_state: UploadResumeState | None = None,
    preserve_split_artifacts: bool = False,
    get_split_meta=None,
    set_split_dest_folder_id=None,
    folder_lock=None,
) -> tuple[list[UploadResult], str | None]:
    from downloader import TransferCancelled

    size = os.path.getsize(src)
    results: list[UploadResult] = []
    needs_split = size > FILESTER_MAX_PART_BYTES
    split_mode = resolve_split_mode(size)
    upload_folder_id = (folder_id or "").strip() or None
    split_progress = (
        on_progress
        if on_progress is not None and hasattr(on_progress, "set_splitting")
        else None
    )

    if resume_dir and resume_state is None:
        resume_state = load_upload_resume_state(resume_dir) or UploadResumeState()
    if resume_state is None:
        resume_state = UploadResumeState()
    if resume_dir:
        resume_state.source_path = src

    skip_part_indices = resume_state.skip_part_indices()
    if skip_part_indices and on_log:
        on_log(
            f"[Filester] Resuming upload — {len(skip_part_indices)} part(s) "
            f"already on Filester; skipping re-upload"
        )

    if not needs_split:
        if resume_state.parts and resume_state.upload_complete():
            results.append(
                _result_from_resume_part(resume_state.parts[0], src, split_mode)
            )
            return results, upload_folder_id
        raw = _upload_filester_file(
            src,
            folder_id=upload_folder_id,
            on_progress=on_progress,
            should_cancel=should_cancel,
            should_restart=should_restart,
            on_log=on_log,
        )
        result = _normalize_filester(raw)
        results.append(result)
        if resume_dir and result.ok:
            slug = filester_upload.file_identifier_from_response(raw)
            if slug:
                resume_state.was_split = False
                resume_state.total_parts = 1
                resume_state.append_part_if_new(
                    UploadedPart(
                        part_index=1,
                        filename=os.path.basename(src),
                        size_bytes=size,
                        slug=slug,
                        upload_response=raw,
                    )
                )
                save_upload_resume_state(resume_dir, resume_state)
        return results, upload_folder_id

    for part in resume_state.parts:
        results.append(
            _result_from_resume_part(
                part,
                src,
                split_mode,
                total_parts=resume_state.total_parts,
            )
        )

    if resume_state.upload_complete():
        dest = (resume_state.split_dest_folder_id or "").strip() or upload_folder_id
        return results, dest or upload_folder_id

    token = (job_id or "").strip() or uuid.uuid4().hex[:8]
    out_dir = os.path.join(os.path.dirname(src) or ".", f".split_{token}")
    os.makedirs(out_dir, exist_ok=True)
    original_basename = os.path.basename(src)
    stem, _ext = os.path.splitext(original_basename)
    fallback_name = stem or "upload"
    split_coord = SplitUploadCoordinator(
        studio_folder_id=upload_folder_id,
        fallback_name=fallback_name,
        get_split_meta=get_split_meta,
        set_split_dest_folder_id=set_split_dest_folder_id,
        folder_lock=folder_lock,
        resume_state=resume_state,
        resume_dir=resume_dir,
        on_log=on_log,
    )
    if upload_folder_id and on_log:
        on_log(
            "[Filester] Progressive split: scene folder when StashDB is ready; "
            "part 1 → studio then moved; later parts → scene folder"
        )
    if on_log:
        if FILESTER_SPLIT_MODE == "optimal" and needs_split:
            on_log(
                f"[Filester] optimal → {split_mode} for {os.path.basename(src)} "
                f"({format_size(size)})"
            )
        mode_label = _split_mode_label(split_mode)
        on_log(
            f"[Filester] {os.path.basename(src)} is {format_size(size)} "
            f"(> {format_size(FILESTER_MAX_PART_BYTES)}); splitting via {mode_label}"
        )
        need_gb = size_limits.required_disk_gb(
            size, FILESTER_MAX_PART_BYTES, split_mode=split_mode
        )
        on_log(f"[Filester] Split upload needs ~{need_gb:.1f} GiB free disk")
        if split_progress is not None:
            split_progress.set_splitting(source_bytes=size)

    def _skip_check():
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")

    def _on_parts_planned(count: int) -> None:
        resume_state.total_parts = count
        resume_state.was_split = count > 1
        if resume_dir:
            save_upload_resume_state(resume_dir, resume_state)

    split_kwargs = {
        "skip_part_indices": skip_part_indices,
        "reuse_existing_parts": bool(skip_part_indices) or bool(resume_dir),
        "on_parts_planned": _on_parts_planned,
    }

    if split_mode == "ffmpeg":
        part_source = file_splitter.iter_upload_parts(
            src,
            FILESTER_MAX_PART_BYTES,
            out_dir,
            on_log=on_log,
            should_cancel=should_cancel,
            delete_source=delete_source and not skip_part_indices,
            ffmpeg_timeout=FILESTER_FFMPEG_TIMEOUT,
            **split_kwargs,
        )
    elif split_mode == "ffmpeg_slice":
        part_source = file_splitter.iter_upload_parts_sliced(
            src,
            FILESTER_MAX_PART_BYTES,
            out_dir,
            on_log=on_log,
            should_cancel=should_cancel,
            delete_source=delete_source and not skip_part_indices,
            ffmpeg_timeout=FILESTER_FFMPEG_TIMEOUT,
            ffprobe_keyframe_timeout=FILESTER_FFPROBE_TIMEOUT,
            extract_backend=SPLITTER_EXTRACT_BACKEND,
            **split_kwargs,
        )
    else:
        part_source = byte_splitter.iter_upload_parts(
            src,
            out_dir,
            FILESTER_MAX_PART_BYTES,
            skip_check=_skip_check,
            delete_source=delete_source and not skip_part_indices,
            on_log=on_log,
            **split_kwargs,
        )

    last_part: dict | None = None
    upload_complete = False
    try:
        for part in part_source:
            if should_cancel and should_cancel():
                raise TransferCancelled("Upload cancelled")
            part_path = part["path"]
            last_part = part
            part_count = int(part.get("part_count") or 1)
            part_index = int(part.get("part_index") or 0)
            if part_index in skip_part_indices:
                continue
            if part_index > 1:
                split_coord.ensure_scene_folder(allow_fallback=True)
            target_folder_id = split_coord.upload_folder_for_part(part_index)
            if split_progress is not None and part_count > 1 and part_index > 0:
                split_progress.register_part(
                    part_index,
                    part.get("filename") or os.path.basename(part_path),
                    int(part.get("size_bytes") or 0),
                    part_count,
                )
            if on_log and not part.get("is_source"):
                dest_hint = (
                    "scene folder"
                    if target_folder_id and target_folder_id != upload_folder_id
                    else "studio folder"
                )
                on_log(
                    f"[Filester] Uploading part {part_index}/{part_count}: "
                    f"{part['filename']} ({format_size(part['size_bytes'])}) → {dest_hint}"
                )
            part_on_progress = on_progress
            if split_progress is not None and part_count > 1 and part_index > 0:
                part_on_progress = split_progress.wrap_part(part_index)
            raw = _upload_filester_file(
                part_path,
                folder_id=target_folder_id,
                on_progress=part_on_progress,
                should_cancel=should_cancel,
                should_restart=should_restart,
                on_log=on_log,
            )
            result = _normalize_filester(raw, part=part)
            if split_progress is not None and part_count > 1 and part_index > 0:
                split_progress.complete_part(part_index)
            split_coord.after_part_uploaded(part_index, raw)
            slug = filester_upload.file_identifier_from_response(raw)
            if resume_dir and result.ok and slug:
                uploaded = UploadedPart(
                    part_index=part_index or 1,
                    filename=part.get("filename") or os.path.basename(part_path),
                    size_bytes=int(part.get("size_bytes") or 0),
                    slug=slug,
                    upload_response=raw,
                )
                if resume_state.append_part_if_new(uploaded):
                    resume_state.was_split = part_count > 1
                    resume_state.total_parts = part_count
                    save_upload_resume_state(resume_dir, resume_state)
            results.append(result)
            if not part.get("is_source"):
                try:
                    os.remove(part_path)
                except OSError:
                    pass

        upload_complete = bool(last_part) and (
            not resume_state.total_parts
            or len(resume_state.parts) >= int(resume_state.total_parts or 0)
        )

        if on_log and last_part and last_part.get("part_count", 1) > 1 and upload_complete:
            stem, ext = os.path.splitext(
                last_part.get("original_basename") or os.path.basename(src)
            )
            original = f"{stem}{ext}"
            mode = last_part.get("split_mode") or split_mode
            if mode in ("ffmpeg", "ffmpeg_slice"):
                if mode == "ffmpeg_slice":
                    rejoin = file_splitter.format_mkvmerge_rejoin_command(
                        stem, ext, last_part["part_count"], output_name=original,
                    )
                    on_log(
                        f"[Filester] Split into {last_part['part_count']} playable parts. "
                        f"Rejoin (phash-identical): {rejoin}"
                    )
                else:
                    on_log(
                        f"[Filester] Split into {last_part['part_count']} playable parts. "
                        f"Rejoin: printf \"file '%s'\\n\" {stem}.PART*{ext} > parts.txt && "
                        f"ffmpeg -f concat -safe 0 -i parts.txt -c copy {original}"
                    )
            else:
                on_log(
                    f"[Filester] Split into {last_part['part_count']} parts. "
                    f"Linux: cat {original}.part* > {original} | "
                    f"Windows: copy /b {original}.part001+...+{original}"
                )
    finally:
        if upload_complete or not preserve_split_artifacts:
            shutil.rmtree(out_dir, ignore_errors=True)
            if resume_dir:
                shutil.rmtree(resume_dir, ignore_errors=True)

    dest_folder = split_coord.dest_folder_id or upload_folder_id
    return results, dest_folder


def upload_source(
    path,
    folder_id=None,
    filester_folder_id=None,
    on_progress=None,
    should_cancel=None,
    should_restart=None,
    on_log=None,
    job_id=None,
    delete_source_after_upload: bool = False,
    resume_dir: str | None = None,
    resume_state: UploadResumeState | None = None,
    preserve_split_artifacts: bool = False,
    get_split_meta=None,
    set_split_dest_folder_id=None,
    folder_lock=None,
) -> tuple[list[UploadResult], str | None, str | None]:
    """Upload a file or directory to all enabled/feasible providers.

    ``folder_id`` is the GoFile folder. ``filester_folder_id`` is resolved by
    the caller from the GoFile folder display name when not supplied.

    When ``delete_source_after_upload`` is True, local source files are removed
    after a successful upload (e.g. temp downloads from link jobs). Path/file-picker
    uploads should leave this False so the user's original file stays on disk.

    Returns (results, filester_skip_reason, filester_folder_id_for_url).
    ``filester_folder_id_for_url`` may be a split-upload subfolder id. Raises if
    every planned destination fails. Filester skip is informational when GoFile still runs.
    """
    from downloader import TransferCancelled

    all_results: list[UploadResult] = []
    filester_skip_reason: str | None = None
    filester_url_folder_id: str | None = (filester_folder_id or "").strip() or None

    for src in _expand_sources(path):
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")

        size = os.path.getsize(src)
        destinations, skip = plan_upload_destinations(size)
        if skip and FILESTER_ENABLED and "filester" not in destinations:
            filester_skip_reason = skip
            if on_log:
                on_log(f"[Filester] Skipped: {skip}")

        if not destinations:
            raise RuntimeError(
                filester_skip_reason or "No upload destination available for this file"
            )

        src_results: list[UploadResult] = []
        gofile_ran = "gofile" in destinations
        filester_ran = "filester" in destinations

        if gofile_ran:
            if on_log:
                on_log(f"[GoFile] Uploading {os.path.basename(src)} ({format_size(size)})")
            src_results.append(
                _upload_gofile(
                    src,
                    folder_id=folder_id,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
            )

        if filester_ran:
            fs_folder = (filester_folder_id or "").strip() or None
            fs_results, effective_fs_folder = _upload_filester_parts(
                src,
                folder_id=fs_folder,
                on_progress=on_progress,
                should_cancel=should_cancel,
                should_restart=should_restart,
                on_log=on_log,
                job_id=job_id,
                delete_source=delete_source_after_upload and not gofile_ran,
                resume_dir=resume_dir,
                resume_state=resume_state,
                preserve_split_artifacts=preserve_split_artifacts,
                get_split_meta=get_split_meta,
                set_split_dest_folder_id=set_split_dest_folder_id,
                folder_lock=folder_lock,
            )
            src_results.extend(fs_results)
            if effective_fs_folder:
                filester_url_folder_id = effective_fs_folder
            if delete_source_after_upload and gofile_ran and os.path.isfile(src):
                try:
                    os.remove(src)
                except OSError:
                    pass

        failed = [r for r in src_results if not r.ok]
        succeeded = [r for r in src_results if r.ok]
        if not succeeded:
            detail = failed[0].raw if failed else "unknown"
            raise RuntimeError(f"All upload destinations failed: {detail}")
        if failed and on_log:
            for r in failed:
                on_log(f"[{r.provider}] Upload failed: {r.raw}")

        all_results.extend(src_results)

    return all_results, filester_skip_reason, filester_url_folder_id
