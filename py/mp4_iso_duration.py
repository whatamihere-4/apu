"""
Parse ISO BMFF (MP4) `mvhd` duration from in-memory buffers (no I/O).

Used by remote OSHASH after HTTP Range fetches. Does not handle WebM/MKV.
"""
from __future__ import annotations

import struct
# Boxes that may contain nested boxes we need to walk to find `mvhd`.
_CONTAINERS = frozenset(
    {
        b"moov",
        b"trak",
        b"mdia",
        b"minf",
        b"stbl",
        b"dinf",
        b"edts",
        b"udta",
        b"mvex",
        b"meta",
        b"moof",
        b"traf",
    }
)


def _read_box_header_declared(
    buf: bytes, off: int, slice_end: int
) -> tuple[int, bytes, int, int] | None:
    """
    Read box header at `off`; return declared payload bounds even if the box
    extends past `slice_end` (truncated buffer). `slice_end` is exclusive.

    Returns (header_len, type, payload_start, declared_end_exclusive).
    """
    if off + 8 > slice_end:
        return None
    size32 = struct.unpack_from(">I", buf, off)[0]
    typ = buf[off + 4 : off + 8]

    if size32 == 0:
        hdr = 8
        declared_end = slice_end
    elif size32 == 1:
        if off + 16 > slice_end:
            return None
        largesize = struct.unpack_from(">Q", buf, off + 8)[0]
        if largesize < 16:
            return None
        hdr = 16
        declared_end = off + largesize
    else:
        if size32 < 8:
            return None
        hdr = 8
        declared_end = off + size32

    pstart = off + hdr
    if pstart > declared_end:
        return None
    return hdr, typ, pstart, declared_end


def _read_box_bounds(buf: bytes, off: int, buf_end: int) -> tuple[int, bytes, int, int] | None:
    """
    Read one box header starting at `off`.

    Returns (header_len, type FourCC, payload_start, box_end_exclusive) relative to buf,
    or None if truncated / invalid.
    """
    if off + 8 > buf_end:
        return None
    size32 = struct.unpack_from(">I", buf, off)[0]
    typ = buf[off + 4 : off + 8]

    if size32 == 0:
        # Box extends to end of enclosing buffer (ISO 14496-12).
        box_end = buf_end
        hdr = 8
    elif size32 == 1:
        if off + 16 > buf_end:
            return None
        largesize = struct.unpack_from(">Q", buf, off + 8)[0]
        if largesize < 16:
            return None
        box_end = off + largesize
        hdr = 16
    else:
        if size32 < 8:
            return None
        box_end = off + size32
        hdr = 8

    if box_end > buf_end or box_end <= off + hdr:
        return None
    return hdr, typ, off + hdr, box_end


def _mvhd_duration_seconds(payload: bytes) -> float | None:
    """Payload is mvhd box interior (after size+type [+extended size])."""
    if len(payload) < 4:
        return None
    ver = payload[0]
    # flags: payload[1:4]
    if ver == 0:
        if len(payload) < 20:
            return None
        timescale = struct.unpack_from(">I", payload, 12)[0]
        duration = struct.unpack_from(">I", payload, 16)[0]
    elif ver == 1:
        if len(payload) < 32:
            return None
        timescale = struct.unpack_from(">I", payload, 20)[0]
        duration = struct.unpack_from(">Q", payload, 24)[0]
    else:
        return None

    if timescale == 0:
        return None
    return duration / float(timescale)


def walk_mvhd_duration(buf: bytes, start: int, end: int) -> float | None:
    """Walk sibling boxes in [start, end). Return first mvhd duration or None."""
    off = start
    while off + 8 <= end:
        parsed = _read_box_bounds(buf, off, end)
        if parsed is not None:
            _hdr, typ, pstart, box_end = parsed
            if typ == b"mvhd":
                d = _mvhd_duration_seconds(buf[pstart:box_end])
                if d is not None and d >= 0:
                    return d
            elif typ in _CONTAINERS:
                inner = walk_mvhd_duration(buf, pstart, box_end)
                if inner is not None:
                    return inner
            off = box_end
            continue

        # Declared box length may extend past `end` (e.g. huge `moov` while we only
        # hold a prefix Range). Still walk children that lie in buf[off:end].
        decl = _read_box_header_declared(buf, off, end)
        if decl is None:
            break
        _hdr, typ, pstart, declared_end = decl
        if pstart > end:
            break
        if typ == b"mvhd":
            avail = buf[pstart : min(declared_end, end)]
            d = _mvhd_duration_seconds(avail)
            if d is not None and d >= 0:
                return d
            break
        if typ in _CONTAINERS:
            inner = walk_mvhd_duration(buf, pstart, end)
            if inner is not None:
                return inner
            break
        break
    return None


def duration_seconds_from_iso_buffer(buf: bytes) -> float | None:
    """
    Parse buffer as BMFF starting at offset 0 (e.g. start of file or start of `moov` payload).

    Returns duration in seconds, or None if no usable `mvhd` found.
    """
    if not buf or len(buf) < 16:
        return None
    return walk_mvhd_duration(buf, 0, len(buf))


def duration_seconds_from_suffix_buffer(buf: bytes) -> float | None:
    """
    Parse a tail slice of a file that may start mid-box (`mdat`) or contain trailing `moov`.

    1) Try a normal walk from offset 0 (works when suffix begins with a box boundary).
    2) Scan for a `moov` FourCC at the usual type offset (size field precedes type).
    """
    d = duration_seconds_from_iso_buffer(buf)
    if d is not None:
        return d

    # Search for 'moov' type field — aligned checks every byte (safe for false positives:
    # we validate box size encloses the buffer slice).
    moov = b"moov"
    i = 0
    lim = len(buf) - 8
    while i <= lim:
        j = buf.find(moov, i, lim + 8)
        if j < 0:
            break
        # type occupies bytes [j, j+4); size at [j-4, j)
        if j < 4:
            i = j + 1
            continue
        box_start = j - 4
        parsed = _read_box_bounds(buf, box_start, len(buf))
        if parsed is not None:
            _hdr, typ, pstart, box_end = parsed
            if typ == b"moov":
                inner = walk_mvhd_duration(buf, pstart, box_end)
                if inner is not None:
                    return inner
            i = j + 1
            continue
        decl = _read_box_header_declared(buf, box_start, len(buf))
        if decl is None:
            i = j + 1
            continue
        _hdr, typ, pstart, declared_end = decl
        if typ != b"moov":
            i = j + 1
            continue
        inner = walk_mvhd_duration(buf, pstart, len(buf))
        if inner is not None:
            return inner
        i = j + 1
    return None
