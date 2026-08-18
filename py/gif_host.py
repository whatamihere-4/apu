"""GIF host selection and per-host SLR preview encode limits."""
from __future__ import annotations

import os
from dataclasses import dataclass

ALLOWED_GIF_HOSTS = ("goonbox", "pixhost", "gifyu")

_HOST_DEFAULTS: dict[str, dict[str, int | str]] = {
    "goonbox": {
        "fps": 12,
        "width": 480,
        "max_duration": 15,
        "max_bytes": 26_214_400,
        "max_animated_pixels": 50_000_000,
        "max_frames": 120,
        "palette_colors": 128,
        "palette_stats": "",
        "dither": "bayer:bayer_scale=3",
    },
    "pixhost": {
        "fps": 12,
        "width": 480,
        "max_duration": 10,
        "max_bytes": 10_485_760,
        "max_animated_pixels": 0,
        "max_frames": 0,
        "palette_colors": 128,
        "palette_stats": "",
        "dither": "bayer:bayer_scale=3",
    },
    "gifyu": {
        # SLR 300p previews are 500×300 @ 24fps × 14s; tuned to ~25 MB with max quality.
        "fps": 24,
        "width": 560,
        "max_duration": 14,
        "max_bytes": 26_214_400,
        "max_animated_pixels": 0,
        "max_frames": 0,
        "palette_colors": 256,
        "palette_stats": "diff",
        "dither": "sierra2_4a",
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
    palette_colors: int
    palette_stats: str  # palettegen stats_mode: full, diff, single (empty = ffmpeg default)
    dither: str  # paletteuse dither= value (e.g. sierra2_4a, bayer:bayer_scale=3, none)

    @property
    def env_prefix(self) -> str:
        return self.host.upper()

    @property
    def label(self) -> str:
        return {"goonbox": "GoonBox", "pixhost": "PiXhost", "gifyu": "Gifyu"}[self.host]

    def ffmpeg_vf(self, fps: int, width: int) -> str:
        """ffmpeg -vf filter for SLR preview MP4 → GIF (palettegen + paletteuse)."""
        colors = max(2, min(self.palette_colors, 256))
        stats = self.palette_stats.strip().lower()
        stats_part = f":stats_mode={stats}" if stats in ("full", "diff", "single") else ""
        dither = (self.dither or "sierra2_4a").strip()
        return (
            f"fps={fps},scale={width}:-1:flags=lanczos,"
            f"split[s0][s1];[s0]palettegen=max_colors={colors}{stats_part}[p];"
            f"[s1][p]paletteuse=dither={dither}"
        )


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw, 10)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip()


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
        fps=_env_int(f"{prefix}_GIF_FPS", int(defaults["fps"])),
        width=_env_int(f"{prefix}_GIF_WIDTH", int(defaults["width"])),
        max_duration=_env_int(f"{prefix}_GIF_MAX_DURATION", int(defaults["max_duration"])),
        max_bytes=_env_int(f"{prefix}_GIF_MAX_BYTES", int(defaults["max_bytes"])),
        max_animated_pixels=_env_int(
            f"{prefix}_GIF_MAX_ANIMATED_PIXELS", int(defaults["max_animated_pixels"])
        ),
        max_frames=_env_int(f"{prefix}_GIF_MAX_FRAMES", int(defaults["max_frames"])),
        palette_colors=_env_int(
            f"{prefix}_GIF_PALETTE_COLORS", int(defaults["palette_colors"])
        ),
        palette_stats=_env_str(
            f"{prefix}_GIF_PALETTE_STATS", str(defaults["palette_stats"])
        ),
        dither=_env_str(f"{prefix}_GIF_DITHER", str(defaults["dither"])),
    )
