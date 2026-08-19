"""Provider-agnostic helpers shared by all upload backends (gofile, filester)."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


def format_size(b):
    if b == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(b, 1024)))
    return f"{b / (1024 ** i):.2f} {units[i]}"


class PhaseTimer:
    """Monotonic elapsed-time helper for upload job logs."""

    def __init__(self, on_log=None, *, prefix: str = "[Filester]"):
        self._on_log = on_log
        self._prefix = prefix
        self._t0 = time.monotonic()
        self._last = self._t0

    def log(self, message: str) -> None:
        if not self._on_log:
            return
        now = time.monotonic()
        total = now - self._t0
        delta = now - self._last
        self._last = now
        self._on_log(f"{self._prefix} +{total:.1f}s (Δ{delta:.1f}s) {message}")


@dataclass
class UploadResult:
    """Normalized result of a single file upload across providers.

    gallery_url is the public download/view page for the uploaded file.
    raw keeps the provider's original JSON response for logging/debugging.
    Split metadata is set when Filester byte-split uploads produce multiple parts.
    """

    ok: bool
    provider: str = ""
    gallery_url: str = ""
    raw: dict = field(default_factory=dict)
    part_index: int = 0
    part_count: int = 1
    original_basename: str = ""
    was_split: bool = False
    split_mode: str = ""
