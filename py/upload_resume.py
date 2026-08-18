"""Persist split/upload progress for upload watchdog resume."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

_STATE_NAME = ".upload-resume.json"


@dataclass
class UploadedPart:
    part_index: int
    filename: str
    size_bytes: int
    slug: str
    upload_response: dict[str, Any] = field(default_factory=dict)
    verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_index": self.part_index,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "slug": self.slug,
            "upload_response": self.upload_response,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UploadedPart:
        return cls(
            part_index=int(data["part_index"]),
            filename=str(data["filename"]),
            size_bytes=int(data["size_bytes"]),
            slug=str(data["slug"]),
            upload_response=data.get("upload_response") or {},
            verified=bool(data.get("verified", True)),
        )


@dataclass
class UploadResumeState:
    oshash: str | None = None
    source_path: str | None = None
    was_split: bool = False
    total_parts: int | None = None
    parts: list[UploadedPart] = field(default_factory=list)
    stashdb_scene_id: str | None = None
    stashdb_title: str | None = None
    stashdb_cover_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "oshash": self.oshash,
            "source_path": self.source_path,
            "was_split": self.was_split,
            "total_parts": self.total_parts,
            "parts": [p.to_dict() for p in self.parts],
            "stashdb_scene_id": self.stashdb_scene_id,
            "stashdb_title": self.stashdb_title,
            "stashdb_cover_path": self.stashdb_cover_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UploadResumeState:
        parts = [UploadedPart.from_dict(p) for p in data.get("parts") or []]
        total = data.get("total_parts")
        return cls(
            oshash=data.get("oshash"),
            source_path=data.get("source_path"),
            was_split=bool(data.get("was_split")),
            total_parts=int(total) if total is not None else None,
            parts=parts,
            stashdb_scene_id=data.get("stashdb_scene_id"),
            stashdb_title=data.get("stashdb_title"),
            stashdb_cover_path=data.get("stashdb_cover_path"),
        )

    def skip_part_indices(self) -> frozenset[int]:
        return frozenset(p.part_index for p in self.parts)

    def part_for_index(self, part_index: int) -> UploadedPart | None:
        for part in self.parts:
            if part.part_index == part_index:
                return part
        return None

    def append_part_if_new(self, part: UploadedPart) -> bool:
        if self.part_for_index(part.part_index):
            return False
        self.parts.append(part)
        return True

    def uploaded_bytes(self) -> int:
        return sum(p.size_bytes for p in self.parts)

    def upload_complete(self) -> bool:
        if self.total_parts is None:
            return False
        return len(self.parts) >= self.total_parts


def resume_job_dir(cache_dir: str, job_id: str) -> str:
    return os.path.join(cache_dir, "upload-resume", (job_id or "").strip())


def state_path(job_dir: str) -> str:
    return os.path.join(job_dir, _STATE_NAME)


def load_upload_resume_state(job_dir: str) -> UploadResumeState | None:
    path = state_path(job_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return UploadResumeState.from_dict(data)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_upload_resume_state(job_dir: str, state: UploadResumeState) -> None:
    os.makedirs(job_dir, exist_ok=True)
    path = state_path(job_dir)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)
    os.replace(tmp, path)


def delete_upload_resume_state(job_dir: str) -> None:
    path = state_path(job_dir)
    try:
        os.remove(path)
    except OSError:
        pass


INTERRUPTED_JOB_FILE = "interrupted_upload.json"


def interrupted_job_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, INTERRUPTED_JOB_FILE)


def save_interrupted_job(cache_dir: str, record: dict) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = interrupted_job_path(cache_dir)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, path)


def load_interrupted_job(cache_dir: str) -> dict | None:
    path = interrupted_job_path(cache_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_interrupted_job(cache_dir: str) -> None:
    path = interrupted_job_path(cache_dir)
    try:
        os.remove(path)
    except OSError:
        pass
