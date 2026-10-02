"""Require actual supported pixels/frames before publishing media metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class MediaInfo:
    width_px: int
    height_px: int
    duration_ms: int | None = None


def preflight_image(stream: BinaryIO, path: Path) -> MediaInfo:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImageReader

    from creator_loop.image_evidence import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS

    if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds local decode budget")
    stream.seek(0)
    raw = stream.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds local decode budget")
    buffer = QBuffer()
    buffer.setData(QByteArray(raw))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    dimensions = reader.size()
    if (
        not dimensions.isValid()
        or dimensions.width() * dimensions.height() > MAX_IMAGE_PIXELS
    ):
        raise ValueError("Image dimensions unavailable or exceed local pixel budget")
    image = reader.read()
    if image.isNull():
        raise ValueError(f"Image cannot be decoded: {reader.errorString()}")
    if image.width() * image.height() > MAX_IMAGE_PIXELS:
        raise ValueError("Image exceeds local pixel budget")
    return MediaInfo(image.width(), image.height())


def preflight_video(stream: BinaryIO, path: Path) -> MediaInfo:
    from PySide6.QtCore import QFile, QIODevice

    from creator_loop.video_evidence import decode_video_path

    device = QFile()
    if not device.open(
        stream.fileno(),
        QIODevice.OpenModeFlag.ReadOnly,
        QFile.FileHandleFlag.DontCloseHandle,
    ):
        raise OSError("Cannot expose held media descriptor to Qt")
    try:
        device.seek(0)
        decoded = decode_video_path(path, device=device, require_audio=True)
    finally:
        device.close()
    return MediaInfo(decoded.image.width(), decoded.image.height(), decoded.duration_ms)
