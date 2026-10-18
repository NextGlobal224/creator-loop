"""Bound, read-only renderer identity for SQLite provenance and cache guards.

No Git invocation, process startup, component download or rendering occurs.
Call this service off the GUI thread; its only media read is the selected font.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import sys
from pathlib import Path
from typing import Any

from creator_loop.backend.publication.publication_media import _open_read_lock
from creator_loop.backend.runtime.local_components import (
    ComponentSpec,
    component_manifest_bytes,
)
from creator_loop.backend.video.audio import audio_output_args
from creator_loop.backend.video.renderer import (
    BASE_ARGS,
    ENCODER_ARGS,
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    _canonical_file,
    _font,
)
from creator_loop.backend.video.tools import MAX_TOOL_OUTPUT_BYTES

IMPLEMENTATION_VERSION = "1.1.0"
IDENTITY_FORMAT = 1
SOURCE_NAMES = (
    "audio.py",
    "planner.py",
    "renderer.py",
    "subtitles.py",
    "timeline.py",
    "tools.py",
    "render_identity.py",
)
SOURCE_PREFIX = "app/creator_loop/backend/video/"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_BUILD_INFO_BYTES = 2 * 1024 * 1024
MAX_FONT_BYTES = 20 * 1024 * 1024
DEFAULT_WORKER_MEMORY = 512 * 1024 * 1024
DEFAULT_RENDER_TIMEOUT = 180


class RendererIdentityError(ValueError):
    pass


def _bounded_file(path: Path, limit: int) -> bytes:
    with _open_read_lock(path) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise RendererIdentityError(
                "Tệp identity không phải file thường hoặc vượt giới hạn."
            )
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (
            len(raw) != info.st_size
            or len(raw) > limit
            or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise RendererIdentityError("Tệp identity đã đổi trong lúc đọc.")
        return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, value in pairs:
        if name in values:
            raise RendererIdentityError("Build identity có trường JSON trùng tên.")
        values[name] = value
    return values


def _source_hashes() -> dict[str, str]:
    if getattr(sys, "frozen", False):
        path = Path(sys.executable).parent / "build-info.json"
        try:
            payload = json.loads(
                _bounded_file(path, MAX_BUILD_INFO_BYTES).decode("utf-8"),
                object_pairs_hook=_unique_object,
            )
            hashes = payload.get("source_hashes") if isinstance(payload, dict) else None
            if not isinstance(hashes, dict):
                raise RendererIdentityError(
                    "Artifact thiếu source_hashes trong build-info.json."
                )
            selected = {
                SOURCE_PREFIX + name: hashes.get(SOURCE_PREFIX + name)
                for name in SOURCE_NAMES
            }
            if any(
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in selected.values()
            ):
                raise RendererIdentityError(
                    "Artifact thiếu fingerprint source renderer đã compile."
                )
            return {name: str(value) for name, value in selected.items()}
        except (
            OSError,
            UnicodeError,
            TypeError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            raise RendererIdentityError(
                "Không xác minh được build identity của renderer đã compile."
            ) from exc
    directory = Path(__file__).resolve(strict=True).parent
    try:
        return {
            SOURCE_PREFIX + name: hashlib.sha256(
                _bounded_file(directory / name, MAX_SOURCE_BYTES).replace(
                    b"\r\n", b"\n"
                )
            ).hexdigest()
            for name in SOURCE_NAMES
        }
    except (OSError, ValueError) as exc:
        raise RendererIdentityError(
            "Không xác minh được source identity của renderer."
        ) from exc


def _font_version(raw: bytes) -> str:
    """Read OpenType name ID 5, never infer a version from the filename."""
    # https://learn.microsoft.com/en-us/typography/opentype/spec/name
    try:
        if len(raw) < 12 or raw[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            raise ValueError("Not a supported sfnt")
        count = struct.unpack_from(">H", raw, 4)[0]
        if not 1 <= count <= 256 or 12 + count * 16 > len(raw):
            raise ValueError("Invalid sfnt directory")
        table = None
        for index in range(count):
            tag, _checksum, offset, length = struct.unpack_from(
                ">4sIII", raw, 12 + index * 16
            )
            if offset > len(raw) or length > len(raw) - offset:
                raise ValueError("Invalid sfnt table range")
            if tag == b"name":
                if table is not None:
                    raise ValueError("Duplicate name table")
                table = raw[offset : offset + length]
        if table is None or len(table) < 6:
            raise ValueError("No name table")
        format_no, records, strings_at = struct.unpack_from(">HHH", table)
        if (
            format_no not in (0, 1)
            or records > 1024
            or 6 + records * 12 > len(table)
            or not 6 + records * 12 <= strings_at <= len(table)
        ):
            raise ValueError("Invalid font name directory")
        candidates: list[tuple[int, str]] = []
        for index in range(records):
            platform, encoding, language, name_id, size, offset = struct.unpack_from(
                ">HHHHHH", table, 6 + index * 12
            )
            if name_id != 5 or platform not in (0, 1, 3):
                continue
            position = strings_at + offset
            if size > 1024 or position > len(table) or size > len(table) - position:
                raise ValueError("Invalid font version range")
            value = (
                table[position : position + size]
                .decode("mac_roman" if platform == 1 else "utf-16-be")
                .strip()
            )
            if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
                raise ValueError("Invalid font version text")
            priority = (
                0
                if platform == 3 and language == 0x409 and encoding in (1, 10)
                else 1
                if platform in (0, 3)
                else 2
            )
            candidates.append((priority, value))
        if not candidates:
            raise ValueError("Missing font version")
        return sorted(candidates)[0][1]
    except (ValueError, UnicodeError, struct.error) as exc:
        raise RendererIdentityError(
            "Font local thiếu metadata version TrueType/OpenType hợp lệ."
        ) from exc


def renderer_identity(specs: tuple[ComponentSpec, ...]) -> dict[str, object]:
    """Snapshot code, pinned tools, effective defaults and the local font.

    Component declarations are not a substitute for the retained execution
    lease. Main render orchestration must compare the sealed identity with the
    actual renderer configuration/font digest before marking success.
    """
    selected = tuple(
        sorted(
            (
                spec
                for spec in specs
                if spec.component_id in {"ffmpeg-exe", "ffprobe-exe"}
            ),
            key=lambda spec: spec.component_id,
        )
    )
    if len(selected) != 2 or {spec.component_id for spec in selected} != {
        "ffmpeg-exe",
        "ffprobe-exe",
    }:
        raise RendererIdentityError("Identity cần đúng ffmpeg-exe và ffprobe-exe.")
    if (
        any(spec.kind != "ENGINE" for spec in selected)
        or selected[0].version != selected[1].version
    ):
        raise RendererIdentityError("Component render phải là ENGINE cùng phiên bản.")
    # Reuse the existing manifest boundary; no execution or binary hashing here.
    pinned = json.loads(component_manifest_bytes(selected))["components"]
    source_hashes = _source_hashes()
    source_fingerprint = hashlib.sha256(
        json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        font_path = _canonical_file(_font(), allow_font_hardlinks=True)
        font_bytes = _bounded_file(font_path, MAX_FONT_BYTES)
    except (OSError, ValueError) as exc:
        raise RendererIdentityError(
            "Không xác minh được font local trong giới hạn 20 MiB."
        ) from exc
    font_digest = hashlib.sha256(font_bytes).hexdigest()
    configuration = {
        "video_codec": "libx264",
        "audio_codec": "aac",
        "pixel_format": "yuv420p",
        "encoder_preset": "ultrafast",
        "crf": 28,
        "maxrate": "2M",
        "threads": 2,
        "worker_memory_limit": DEFAULT_WORKER_MEMORY,
        "timeout_seconds": DEFAULT_RENDER_TIMEOUT,
        "components": {
            spec.component_id: {"version": spec.version, "sha256": spec.sha256}
            for spec in selected
        },
        "font_sha256": font_digest,
    }
    return {
        "identity_format": IDENTITY_FORMAT,
        "implementation_version": IMPLEMENTATION_VERSION,
        "source_hashes": source_hashes,
        "source_fingerprint": source_fingerprint,
        "font_path": str(font_path),
        "font_sha256": font_digest,
        "font_byte_size": len(font_bytes),
        "font_version": _font_version(font_bytes),
        "pinned_components": pinned,
        "configuration": configuration,
        "encoder_args": list(ENCODER_ARGS),
        "base_args": list(BASE_ARGS),
        "audio_args_for_one_second": audio_output_args(1000),
        "budgets": {
            "max_input_bytes": MAX_INPUT_BYTES,
            "max_output_bytes": MAX_OUTPUT_BYTES,
            "max_tool_output_bytes": MAX_TOOL_OUTPUT_BYTES,
            "max_font_bytes": MAX_FONT_BYTES,
            "worker_memory_bytes": DEFAULT_WORKER_MEMORY,
            "render_timeout_seconds": DEFAULT_RENDER_TIMEOUT,
        },
    }
