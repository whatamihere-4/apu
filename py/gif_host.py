"""GIF host selection and per-host SLR preview encode limits."""
from __future__ import annotations

import os
from dataclasses import dataclass

ALLOWED_GIF_HOSTS = ("goonbox", "pixhost", "gifyu")

_HOST_DEFAULTS: dict[str, dict[str, int]] = {
    "goonbox": {
        "fps": 12,
        "width": 480,
        "max_duration": 15,
        "max_bytes": 26_214_400,
        "max_animated_pixels": 50_000_000,
        "max_frames": 120,
    },
    "pixhost": {
        "fps": 12,
        "width": 480,
        "max_duration": 10,
        "max_bytes": 10_485_760,
        "max_animated_pixels": 0,
        "max_frames": 0,
    },
    "gifyu": {
        "fps": 12,
        "width": 720,
        "max_duration": 30,
        "max_bytes": 104_857_600,
        "max_animated_pixels": 0,
        "max_frames": 0,
    },
}


@dataclass(frozen=True)
class GifEncodeLimits:
    host: str
    fps: int
    width: int
    max_duration: int
    max_bytes: int
    max_animated_pixels: int  # 0 = no animated-pixel cap
    max_frames: int  # 0 = no frame cap

    @property
    def env_prefix(self) -> str:
        return self.host.upper()

    @property
    def label(self) -> str:
        return {"goonbox": "GoonBox", "pixhost": "PiXhost", "gifyu": "Gifyu"}[self.host]


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw, 10)
    except ValueError:
        return default


def normalize_gif_host(raw: str | None) -> str:
    host = (raw or "goonbox").strip().lower()
    if host not in ALLOWED_GIF_HOSTS:
        allowed = ", ".join(ALLOWED_GIF_HOSTS)
        raise ValueError(f"GIF_HOST must be one of: {allowed} (got {raw!r})")
    return host


def resolved_gif_host() -> str:
    return normalize_gif_host(os.environ.get("GIF_HOST"))


def gif_encode_limits(host: str | None = None) -> GifEncodeLimits:
    """Encode limits for ``GIF_HOST`` (or explicit *host*)."""
    h = normalize_gif_host(host or os.environ.get("GIF_HOST"))
    defaults = _HOST_DEFAULTS[h]
    prefix = h.upper()
    return GifEncodeLimits(
        host=h,
        fps=_env_int(f"{prefix}_GIF_FPS", defaults["fps"]),
        width=_env_int(f"{prefix}_GIF_WIDTH", defaults["width"]),
        max_duration=_env_int(f"{prefix}_GIF_MAX_DURATION", defaults["max_duration"]),
        max_bytes=_env_int(f"{prefix}_GIF_MAX_BYTES", defaults["max_bytes"]),
        max_animated_pixels=_env_int(
            f"{prefix}_GIF_MAX_ANIMATED_PIXELS", defaults["max_animated_pixels"]
        ),
        max_frames=_env_int(f"{prefix}_GIF_MAX_FRAMES", defaults["max_frames"]),
    )
