"""Pure, bounded timeline validation shared by planning and local rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass

from creator_loop.shared.workflow import RenderPreset, Scene, Subtitle, VerifiedMedia


class VideoPlanError(ValueError):
    pass


@dataclass(frozen=True)
class TimedScene:
    scene: Scene
    start_ms: int
    duration_ms: int
    frames: int


@dataclass(frozen=True)
class Timeline:
    scenes: tuple[TimedScene, ...]
    duration_ms: int
    frames: int


def media_kind(media: VerifiedMedia) -> str:
    value = media.media_type.upper()
    for kind in ("IMAGE", "VIDEO", "AUDIO"):
        if value == kind or value.startswith(kind + "/"):
            return kind
    raise VideoPlanError(
        "Tư liệu render phải là file ảnh, video hoặc âm thanh đã nhập."
    )


def validate_preset(preset: RenderPreset) -> None:
    if (
        not preset.name.strip()
        or len(preset.name) > 80
        or type(preset.width) is not int
        or type(preset.height) is not int
        or not 128 <= preset.width <= 1920
        or not 128 <= preset.height <= 1920
        or preset.width % 2
        or preset.height % 2
        or preset.width * preset.height > 1920 * 1080
        or type(preset.fps) is not int
        or not 12 <= preset.fps <= 30
        or type(preset.max_duration_ms) is not int
        or not 1000 <= preset.max_duration_ms <= 180000
    ):
        raise VideoPlanError(
            "Preset vượt phạm vi local: tối đa 1080p, 30 fps và 180 giây."
        )


def build_timeline(
    scenes: tuple[Scene, ...],
    preset: RenderPreset,
    media: tuple[VerifiedMedia, ...] = (),
    subtitles: tuple[Subtitle, ...] = (),
    voiceover_file_id: str | None = None,
) -> Timeline:
    """Require explicit media rights and validate before executing any tool.

    Durations round up to complete frames. Missing or URL-only inputs never
    become a scene. This function performs no file I/O or background work.
    """
    validate_preset(preset)
    if not 1 <= len(scenes) <= 60 or len(media) > 100:
        raise VideoPlanError("Cần 1–60 cảnh và tối đa 100 file tư liệu.")
    by_id: dict[str, VerifiedMedia] = {}
    for item in media:
        if not item.file_id or item.file_id in by_id:
            raise VideoPlanError("File tư liệu trùng ID hoặc thiếu ID.")
        by_id[item.file_id] = item
    timed: list[TimedScene] = []
    frame_count = 0
    for scene in scenes:
        if (
            scene.kind not in ("TITLE", "IMAGE", "VIDEO")
            or type(scene.duration_ms) is not int
            or not 500 <= scene.duration_ms <= 30000
            or type(scene.clip_start_ms) is not int
            or not 0 <= scene.clip_start_ms <= 24 * 60 * 60 * 1000
            or not isinstance(scene.text, str)
            or len(scene.text) > 1200
            or "\x00" in scene.text
        ):
            raise VideoPlanError(
                "Cảnh có loại, thời lượng, vị trí cắt hoặc chữ không hợp lệ."
            )
        if scene.kind == "TITLE":
            if (
                scene.file_id is not None
                or scene.clip_start_ms
                or not scene.text.strip()
            ):
                raise VideoPlanError(
                    "Cảnh chữ cần nội dung, không có file hoặc vị trí cắt."
                )
        else:
            matched = by_id.get(scene.file_id or "")
            if (
                matched is None
                or not matched.reuse_allowed
                or media_kind(matched) != scene.kind
            ):
                raise VideoPlanError("Cảnh cần đúng file đã nhập và quyền REUSE_MEDIA.")
            if scene.kind == "IMAGE" and scene.clip_start_ms:
                raise VideoPlanError("Ảnh không có vị trí cắt video.")
        frames = math.ceil(scene.duration_ms * preset.fps / 1000)
        start_ms = round(frame_count * 1000 / preset.fps)
        frame_count += frames
        end_ms = round(frame_count * 1000 / preset.fps)
        timed.append(TimedScene(scene, start_ms, end_ms - start_ms, frames))
    duration_ms = round(frame_count * 1000 / preset.fps)
    if duration_ms > preset.max_duration_ms:
        raise VideoPlanError(
            "Tổng thời lượng cảnh vượt preset; hãy rút ngắn kịch bản hoặc đổi preset."
        )
    if voiceover_file_id is not None:
        audio = by_id.get(voiceover_file_id)
        if audio is None or not audio.reuse_allowed or media_kind(audio) != "AUDIO":
            raise VideoPlanError(
                "Lời đọc cần file âm thanh đã nhập và quyền REUSE_MEDIA."
            )
    if len(subtitles) > 300:
        raise VideoPlanError("Tối đa 300 dòng phụ đề cho một bản render.")
    previous_end = 0
    for subtitle in subtitles:
        if (
            type(subtitle.start_ms) is not int
            or type(subtitle.end_ms) is not int
            or not previous_end <= subtitle.start_ms < subtitle.end_ms <= duration_ms
            or not subtitle.text.strip()
            or len(subtitle.text) > 1000
            or "\x00" in subtitle.text
        ):
            raise VideoPlanError(
                "Phụ đề cần chữ và mốc thời gian liên tiếp trong timeline."
            )
        previous_end = subtitle.end_ms
    return Timeline(tuple(timed), duration_ms, frame_count)


def subtitles_from_timeline(timeline: Timeline) -> tuple[Subtitle, ...]:
    return tuple(
        Subtitle(item.start_ms, item.start_ms + item.duration_ms, item.scene.text)
        for item in timeline.scenes
        if item.scene.text.strip()
    )
