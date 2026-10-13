"""Local script-to-scene planning, with no inference or media processing."""

from __future__ import annotations

import math
import re

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
    """Create editable scenes from the exact Draft Version's plain script.

    Only explicitly permitted imported images are used automatically. Videos
    require an explicit cut range from the scene editor; their content is never
    guessed from a caption. No evidence acceptance or human selection occurs.
    """
    if draft.format.upper() != "VIDEO" or not draft.body_text.strip():
        raise VideoPlanError("Lập cảnh cần Draft video có kịch bản.")
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
