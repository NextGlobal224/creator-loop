"""Video component coordination using the existing verified component manager."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
from typing import Callable
from uuid import uuid4

from creator_loop.backend.runtime.component_selection import load_component_selection
from creator_loop.backend.runtime.database import _connect_write, validate
from creator_loop.backend.runtime.local_components import (
    ComponentSpec,
    component_manifest_bytes,
    hold_verified_components,
    load_component_manifest,
)

VIDEO_COMPONENTS = frozenset({"ffmpeg-exe", "ffprobe-exe"})


def load_video_components(root: Path) -> tuple[ComponentSpec, ...]:
    path = root / "manifests" / "local-video-components.json"
    if path.exists():
        specs = load_component_manifest(path)
    else:
        selected = load_component_selection(root)
        specs = (
            tuple(s for s in selected.specs if s.component_id in VIDEO_COMPONENTS)
            if selected
            else ()
        )
    if {s.component_id for s in specs} != VIDEO_COMPONENTS or len(specs) != 2:
        raise ValueError("Chưa cấu hình FFmpeg và ffprobe local đã xác minh.")
    if any(s.kind != "ENGINE" or not s.local_use_allowed for s in specs):
        raise ValueError("Component video phải là engine được phép chạy local.")
    return specs


def configure_video_components(
    root: Path, manifest: Path, cancelled: Callable[[], bool] = lambda: False
) -> tuple[ComponentSpec, ...]:
    specs = load_component_manifest(manifest)
    if {s.component_id for s in specs} != VIDEO_COMPONENTS or len(specs) != 2:
        raise ValueError("Manifest video cần đúng ffmpeg-exe và ffprobe-exe.")
    if any(s.owned for s in specs):
        raise ValueError("Lựa chọn file ngoài app không chuyển ownership cho app.")
    with hold_verified_components(
        specs,
        worker_memory_limit=512 * 1024**2,
        timeout_seconds=120,
        cancelled=cancelled,
    ):
        folder = (root / "manifests").resolve(strict=True)
        if (
            not folder.is_relative_to(root.resolve())
            or folder.is_symlink()
            or folder.is_junction()
        ):
            raise ValueError("Manifest phải ở data root thật.")
        path = folder / "local-video-components.json"
        pending = folder / ("video-selection-" + uuid4().hex + ".pending")
        payload = component_manifest_bytes(specs)
        with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
            db.execute("BEGIN IMMEDIATE")
            validate(db)
            if cancelled():
                raise InterruptedError("Đã Hủy cấu hình video.")
            if path.exists():
                if path.is_symlink() or path.is_junction():
                    raise ValueError("Manifest cũ có alias.")
                backup = folder / ("video-selection-prior-" + uuid4().hex + ".json")
                with backup.open("xb") as stream:
                    stream.write(path.read_bytes())
            try:
                with pending.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if cancelled():
                    raise InterruptedError("Đã Hủy cấu hình video.")
                os.replace(pending, path)
                db.commit()
            finally:
                pending.unlink(missing_ok=True)
    return specs
