"""Provider-agnostic helpers shared by all upload backends (gofile, filester)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def format_size(b):
    if b == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(b, 1024)))
    return f"{b / (1024 ** i):.2f} {units[i]}"


@dataclass
class UploadResult:
    """Normalized result of a single file upload across providers.

    gallery_url is the public download/view page for the uploaded file.
    raw keeps the provider's original JSON response for logging/debugging.
    """

    ok: bool
    gallery_url: str = ""
    raw: dict = field(default_factory=dict)
