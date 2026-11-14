"""Conservative container checks for serving retained originals to a player.

These checks identify a container, not decodability, duration or ASR readiness.
They never trust a user-supplied filename or MIME type.
"""

from __future__ import annotations


def _mp3_frame(raw: bytes, offset: int) -> int:
    if offset + 4 > len(raw):
        return 0
    header = int.from_bytes(raw[offset : offset + 4], "big")
    version, layer = (header >> 19) & 3, (header >> 17) & 3
    bitrate, sample_rate = (header >> 12) & 15, (header >> 10) & 3
    if (
        header >> 21 != 0x7FF
        or version == 1
        or layer != 1
        or bitrate in (0, 15)
        or sample_rate == 3
    ):
        return 0
    rates = (44100, 48000, 32000)
    rate = rates[sample_rate] // ({3: 1, 2: 2, 0: 4}[version])
    table = (
        (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        if version == 3
        else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    )
    length = (144 if version == 3 else 72) * table[bitrate] * 1000 // rate
    length += (header >> 9) & 1
    return length if length >= 4 and offset + length <= len(raw) else 0


def _is_mp3(raw: bytes) -> bool:
    pos = 0
    if raw.startswith(b"ID3"):
        if len(raw) < 10 or raw[3] not in (2, 3, 4) or any(x & 0x80 for x in raw[6:10]):
            return False
        size = 0
        for byte in raw[6:10]:
            size = (size << 7) | byte
        pos = 10 + size + (10 if raw[3] == 4 and raw[5] & 0x10 else 0)
    first = _mp3_frame(raw, pos)
    return bool(first and _mp3_frame(raw, pos + first))


def _boxes(raw: bytes, start: int, end: int):
    pos = start
    count = 0
    while pos < end:
        count += 1
        if count > 4096:
            raise ValueError("too many MP4 boxes")
        if end - pos < 8:
            raise ValueError("truncated MP4 box")
        size = int.from_bytes(raw[pos : pos + 4], "big")
        kind = raw[pos + 4 : pos + 8]
        header = 8
        if size == 1:
            if end - pos < 16:
                raise ValueError("truncated MP4 extended size")
            size, header = int.from_bytes(raw[pos + 8 : pos + 16], "big"), 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError("invalid MP4 box size")
        yield kind, pos + header, pos + size
        pos += size


def _is_m4a(raw: bytes) -> bool:
    try:
        top = list(_boxes(raw, 0, len(raw)))
        ftyp = [(a, b) for kind, a, b in top if kind == b"ftyp"]
        moov = [(a, b) for kind, a, b in top if kind == b"moov"]
        if (
            len(ftyp) != 1
            or len(moov) != 1
            or not any(kind == b"mdat" and b > a for kind, a, b in top)
        ):
            return False
        a, b = ftyp[0]
        if b - a < 8 or (b - a - 8) % 4:
            return False
        brands = {raw[a : a + 4]} | {raw[i : i + 4] for i in range(a + 8, b, 4)}
        if not brands.intersection(
            {b"M4A ", b"M4B ", b"isom", b"iso2", b"mp41", b"mp42"}
        ):
            return False
        handlers = []
        for kind, a, b in _boxes(raw, *moov[0]):
            if kind != b"trak":
                continue
            track_handlers = []
            for name, c, d in _boxes(raw, a, b):
                if name == b"mdia":
                    for typ, e, f in _boxes(raw, c, d):
                        if typ == b"hdlr":
                            if f - e < 12:
                                return False
                            track_handlers.append(raw[e + 8 : e + 12])
            if len(track_handlers) != 1:
                return False
            handlers.extend(track_handlers)
        return (
            bool(handlers)
            and b"soun" in handlers
            and all(kind == b"soun" for kind in handlers)
        )
    except ValueError:
        return False


def playback_audio_type(raw: bytes) -> str | None:
    """Return a conservative MP3/M4A MIME; unknown content remains download-only."""
    if _is_mp3(raw):
        return "audio/mpeg"
    if _is_m4a(raw):
        return "audio/mp4"
    return None
