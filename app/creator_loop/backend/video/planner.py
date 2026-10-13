"""Local script-to-scene planning, with no inference or media processing."""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal

from creator_loop.backend.video.timeline import (
    VideoPlanError,
    build_timeline,
    media_kind,
)
from creator_loop.shared.workflow import (
    DraftSnapshot,
    RenderPreset,
    Scene,
    VerifiedMedia,
)

_LABEL = re.compile(
    r"^(?:(?:cảnh|scene)\s*\d+|hook|mở đầu|lời đọc|lời dẫn|narration|kết|kết luận|cta)\s*[:：–-]\s*",
    re.IGNORECASE,
)
_DECLARED_SCENE = re.compile(
    r"([1-9][0-9]*)[.)]\s*(Tiêu đề|Chữ)\s*:\s*(.+?)\s*—\s*([0-9]{1,3}(?:[.,][0-9]{1,3})?)\s+giây",
    re.IGNORECASE,
)


def _declared_scenes(script: str) -> tuple[Scene, ...] | None:
    """Creator's explicit section convention, without rewriting its Draft.

    Only KẾ HOẠCH CẢNH is rendered. Context, full narration, citations and review
    notices stay in the immutable Draft. Narration text is not synthetic speech;
    a voiceover requires an imported file passed in RenderRequest.
    """
    lines = [unicodedata.normalize("NFC", line).strip() for line in script.splitlines()]
    nonempty = [line for line in lines if line]
    if not nonempty or nonempty[0].casefold() != "kịch bản video nháp":
        return None
    folded = [line.casefold() for line in lines]
    if folded.count("lời đọc") != 1 or folded.count("kế hoạch cảnh") != 1:
        raise VideoPlanError(
            "Draft theo mẫu cần đúng một mục LỜI ĐỌC và KẾ HOẠCH CẢNH."
        )
    narration_at = folded.index("lời đọc")
    scenes_at = folded.index("kế hoạch cảnh")
    if narration_at >= scenes_at or not any(lines[narration_at + 1 : scenes_at]):
        raise VideoPlanError("Mẫu Draft thiếu lời đọc trước kế hoạch cảnh.")
    end = next(
        (
            index
            for index in range(scenes_at + 1, len(lines))
            if folded[index].startswith(
                (
                    "nguồn nghiên cứu — exact claim versions",
                    "bản nháp editorial theo mẫu local",
                )
            )
        ),
        len(lines),
    )
    scenes: list[Scene] = []
    for line in lines[scenes_at + 1 : end]:
        if not line:
            continue
        matched = _DECLARED_SCENE.fullmatch(line)
        if matched is None or int(matched[1]) != len(scenes) + 1:
            raise VideoPlanError(
                "Kế hoạch cảnh cần số thứ tự liên tiếp, Tiêu đề/Chữ, nội dung và thời lượng '— N giây'; media cần chọn bằng trình sửa cảnh."
            )
        duration_ms = int(Decimal(matched[4].replace(",", ".")) * 1000)
        scenes.append(Scene(matched[3].strip(), duration_ms, "TITLE"))
    if not scenes:
        raise VideoPlanError("Kế hoạch cảnh rỗng; chưa có nội dung để render.")
    return tuple(scenes)


def _units(script: str) -> list[str]:
    cleaned = [
        _LABEL.sub("", line.strip()).strip()
        for line in script.splitlines()
        if line.strip()
    ]
    result: list[str] = []
    for paragraph in cleaned:
        words = paragraph.split()
        while words:
            # Keep every word; split long paragraphs rather than silently crop.
            chunk: list[str] = []
            while words and len(" ".join([*chunk, words[0]])) <= 220:
                chunk.append(words.pop(0))
            if not chunk:
                raise VideoPlanError(
                    "Kịch bản có từ quá dài để hiển thị trong preview."
                )
            result.append(" ".join(chunk))
    return result


def plan_draft(
    draft: DraftSnapshot,
    media: tuple[VerifiedMedia, ...] = (),
    preset: RenderPreset = RenderPreset(),
) -> tuple[Scene, ...]:
    """Create editable scenes from the exact Draft Version.

    Only explicitly permitted imported images are used automatically. Videos
    require an explicit cut range from the scene editor; their content is never
    guessed from a caption. No evidence acceptance or human selection occurs.
    Structured Creator Drafts use their explicitly declared scene section;
    arbitrary plain scripts retain every word rather than discard paragraphs.
    """
    if draft.format.upper() != "VIDEO" or not draft.body_text.strip():
        raise VideoPlanError("Lập cảnh cần Draft video có kịch bản.")
    declared = _declared_scenes(draft.body_text)
    if declared is not None:
        build_timeline(declared, preset, media)
        return declared
    paragraphs = _units(draft.body_text)
    if len(paragraphs) > 60:
        raise VideoPlanError("Kịch bản cần hơn 60 cảnh; hãy chia thành Draft nhỏ hơn.")
    images = [
        item for item in media if item.reuse_allowed and media_kind(item) == "IMAGE"
    ]
    scenes = tuple(
        Scene(
            text=text,
            duration_ms=min(30000, max(3000, math.ceil(len(text.split()) / 3) * 1000)),
            kind="IMAGE" if images else "TITLE",
            file_id=images[index % len(images)].file_id if images else None,
        )
        for index, text in enumerate(paragraphs)
    )
    build_timeline(scenes, preset, media)
    return scenes
