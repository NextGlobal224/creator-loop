"""Immutable interfaces for research, Creator and local rendering. No execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Protocol

Mode = Literal["AUTOMATIC", "MANUAL"]
OutputFormat = Literal["TEXT", "IMAGE_CAPTION", "VIDEO"]
RenderStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]


@dataclass(frozen=True)
class ResearchMaterial:
    material_id: str
    source_id: str | None
    asset_id: str | None
    file_id: str | None
    sha256: str | None
    media_type: str
    text: str
    source_url: str | None = None
    evidence_version_ids: tuple[str, ...] = ()
    scope: str = "CAPTION_ONLY"


@dataclass(frozen=True)
class CreatorRequest:
    project_id: str
    mode: Mode
    purpose: str
    audience: str
    style: str
    output_format: OutputFormat
    material_ids: tuple[str, ...]
    direction: str = ""
    actor: str = "creator"


@dataclass(frozen=True)
class DraftSnapshot:
    draft_version_id: str
    project_id: str
    version_no: int
    body_text: str
    format: str
    claim_version_ids: tuple[str, ...] = ()
    direction: str = ""
    image_file_id: str | None = None
    image_sha256: str | None = None
    image_provenance: str = ""
    image_usage_note: str = ""


@dataclass(frozen=True)
class VerifiedMedia:
    file_id: str
    asset_id: str
    path: Path
    sha256: str
    media_type: str
    reuse_allowed: bool = False


@dataclass(frozen=True)
class Scene:
    text: str
    duration_ms: int = 3000
    kind: Literal["TITLE", "IMAGE", "VIDEO"] = "TITLE"
    file_id: str | None = None
    clip_start_ms: int = 0


@dataclass(frozen=True)
class Subtitle:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class RenderPreset:
    name: str = "preview-vertical"
    width: int = 360
    height: int = 640
    fps: int = 24
    max_duration_ms: int = 60000


@dataclass(frozen=True)
class RenderRequest:
    run_id: str
    draft: DraftSnapshot
    scenes: tuple[Scene, ...]
    output_directory: Path
    media: tuple[VerifiedMedia, ...] = ()
    subtitles: tuple[Subtitle, ...] = ()
    voiceover_file_id: str | None = None
    preset: RenderPreset = field(default_factory=RenderPreset)


@dataclass(frozen=True)
class Progress:
    run_id: str
    status: RenderStatus
    completed: int
    total: int
    message: str


@dataclass(frozen=True)
class RenderResult:
    run_id: str
    draft_version_id: str
    status: RenderStatus
    output_path: Path | None = None
    sha256: str | None = None
    byte_size: int | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    tool_name: str = "ffmpeg"
    tool_version: str = ""
    error: str | None = None


class Renderer(Protocol):
    def render(
        self,
        request: RenderRequest,
        progress: Callable[[Progress], None],
        cancelled: Callable[[], bool],
    ) -> RenderResult: ...


class CreatorPipeline(Protocol):
    def analyze(self, request: CreatorRequest) -> str: ...
    def create_draft(self, workflow_id: str, direction: str = "") -> DraftSnapshot: ...
