"""Unit tests for ISO BMFF mvhd duration parsing (no pytest dependency)."""
from __future__ import annotations

import struct
import unittest
import sys
from pathlib import Path

PY_DIR = Path(__file__).resolve().parent
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from mp4_iso_duration import (
    duration_seconds_from_iso_buffer,
    duration_seconds_from_suffix_buffer,
)


def _be_u32(n: int) -> bytes:
    return struct.pack(">I", n)


def _box(typ: bytes, payload: bytes) -> bytes:
    return _be_u32(8 + len(payload)) + typ + payload


def _mvhd_v0(timescale: int, duration_ticks: int) -> bytes:
    fullbox = struct.pack(">B", 0) + b"\x00\x00\x00"
    body = _be_u32(0) + _be_u32(0) + _be_u32(timescale) + _be_u32(duration_ticks)
    return _box(b"mvhd", fullbox + body)


def _mvhd_v1(timescale: int, duration_ticks: int) -> bytes:
    fullbox = struct.pack(">B", 1) + b"\x00\x00\x00"
    body = (b"\x00" * 16) + _be_u32(timescale) + struct.pack(">Q", duration_ticks)
    return _box(b"mvhd", fullbox + body)


class Mp4IsoDurationTests(unittest.TestCase):
    def test_mvhd_v0_from_moov_root(self):
        moov = _box(b"moov", _mvhd_v0(1000, 5000))
        d = duration_seconds_from_iso_buffer(moov)
        self.assertAlmostEqual(d, 5.0)

    def test_mvhd_v1(self):
        moov = _box(b"moov", _mvhd_v1(60000, 300000))
        d = duration_seconds_from_iso_buffer(moov)
        self.assertAlmostEqual(d, 5.0)

    def test_ftyp_then_moov(self):
        ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isom")
        moov = _box(b"moov", _mvhd_v0(1000, 3000))
        buf = ftyp + moov
        d = duration_seconds_from_iso_buffer(buf)
        self.assertAlmostEqual(d, 3.0)

    def test_moov_found_in_suffix_after_junk(self):
        junk = b"\x01\x02" * 400
        moov = _box(b"moov", _mvhd_v0(500, 2500))
        buf = junk + moov
        self.assertIsNone(duration_seconds_from_iso_buffer(buf))
        d = duration_seconds_from_suffix_buffer(buf)
        self.assertAlmostEqual(d, 5.0)

    def test_truncated_moov_declared_larger_than_buffer(self):
        """Real files: `moov` can be multi-MiB; only a prefix is in a Range buffer."""
        ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isom")
        mvhd = _mvhd_v0(1000, 42_000)  # 42 s
        moov_payload = mvhd
        moov_lie_size = 2_000_000
        moov = _be_u32(moov_lie_size) + b"moov" + moov_payload
        buf = ftyp + moov
        self.assertLess(len(buf), moov_lie_size)
        d = duration_seconds_from_iso_buffer(buf)
        self.assertAlmostEqual(d, 42.0)


if __name__ == "__main__":
    unittest.main()
