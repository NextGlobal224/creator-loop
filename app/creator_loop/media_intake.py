"""Import supported video and image originals from their stored byte signatures."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from creator_loop.text_intake import ImportedOriginal, _intake_original


def _video_type(stored: BinaryIO) -> tuple[str, str]:
    header = stored.read(12)
    mp4_brands = {b"isom", b"iso2", b"avc1", b"mp41", b"mp42", b"M4V "}
    if (
        len(header) >= 12
        and int.from_bytes(header[:4], "big") >= 16
        and header[4:8] == b"ftyp"
        and header[8:12] in mp4_brands
    ):
        return "VIDEO", "video/mp4"
    raise ValueError("Unsupported video original: expected MP4 file signature")


def _image_type(stored: BinaryIO) -> tuple[str, str]:
    header = stored.read(8)
    if header == b"\x89PNG\r\n\x1a\n":
        return "IMAGE", "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "IMAGE", "image/jpeg"
    raise ValueError("Unsupported image original: expected PNG or JPEG signature")


def intake_video_original(
    source_path: Path, *, root: Path | None = None
) -> ImportedOriginal:
    """Register a physical MP4 original after signature and byte verification."""
    return _intake_original(source_path, classify=_video_type, root=root)


def intake_image_original(
    source_path: Path, *, root: Path | None = None
) -> ImportedOriginal:
    """Register a physical PNG or JPEG original after signature verification."""
    return _intake_original(source_path, classify=_image_type, root=root)
