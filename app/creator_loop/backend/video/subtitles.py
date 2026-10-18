"""UTF-8 text/subtitle files; user text never becomes FFmpeg filter syntax."""

from __future__ import annotations

import textwrap
from pathlib import Path

from creator_loop.backend.video.timeline import VideoPlanError
from creator_loop.shared.workflow import RenderPreset, Subtitle


def wrapped_text(text: str, columns: int) -> str:
    return "\n".join(
        row
        for paragraph in text.replace("\r", "").split("\n")
        for row in (
            textwrap.wrap(paragraph, width=columns, break_long_words=True) or [""]
        )
    )


def _layout(text: str, preset: RenderPreset, *, title: bool) -> tuple[str, int]:
    size = max(
        14,
        min(
            preset.width // (15 if title else 18),
            preset.height // (20 if title else 26),
        ),
    )
    available_height = preset.height - 72 if title else preset.height // 3
    while size >= 14:
        columns = max(4, (preset.width - 40) // size)
        wrapped = wrapped_text(text, columns)
        if len(wrapped.splitlines()) * (size + 6) <= available_height:
            return wrapped, size
        size -= 1
    raise VideoPlanError(
        "Chữ/phụ đề quá dài cho cảnh preview; hãy chia thành cảnh hoặc dòng ngắn hơn."
    )


def write_scene_text(
    workspace: Path, number: int, text: str, preset: RenderPreset
) -> tuple[str, int]:
    name = f"scene-{number:03d}.txt"
    wrapped, size = _layout(text, preset, title=True)
    (workspace / name).write_text(wrapped, encoding="utf-8")
    return name, size


def text_filter(
    filename: str,
    preset: RenderPreset,
    *,
    title: bool = False,
    font_size: int | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> str:
    """Filenames are generated ASCII basenames, never supplied by the user."""
    if (
        not filename.endswith(".txt")
        or not filename.replace("-", "").replace(".", "").isalnum()
    ):
        raise ValueError("Generated text filename required")
    size = font_size or max(
        14,
        min(
            preset.width // (15 if title else 18),
            preset.height // (20 if title else 26),
        ),
    )
    y = "(h-text_h)/2" if title else "h-text_h-36"
    result = (
        f"drawtext=fontfile=font.ttf:textfile={filename}:expansion=none:"
        f"fontsize={size}:fontcolor=white:line_spacing=6:box=1:"
        f"boxcolor=black@0.68:boxborderw=10:x=(w-text_w)/2:y={y}"
    )
    if start_ms is not None and end_ms is not None:
        # The numeric timeline is validated before this filter is generated.
        result += f":enable='gte(t,{start_ms / 1000:.3f})*lt(t,{end_ms / 1000:.3f})'"
    return result


def subtitle_filters(
    workspace: Path, subtitles: tuple[Subtitle, ...], preset: RenderPreset
) -> tuple[str, ...]:
    filters: list[str] = []
    for index, item in enumerate(subtitles):
        name = f"subtitle-{index:03d}.txt"
        wrapped, size = _layout(item.text, preset, title=False)
        (workspace / name).write_text(wrapped, encoding="utf-8")
        filters.append(
            text_filter(
                name, preset, font_size=size, start_ms=item.start_ms, end_ms=item.end_ms
            )
        )
    return tuple(filters)


def _srt_time(milliseconds: int) -> str:
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def write_srt(path: Path, subtitles: tuple[Subtitle, ...]) -> None:
    path.write_text(
        "\n\n".join(
            f"{index}\n{_srt_time(item.start_ms)} --> {_srt_time(item.end_ms)}\n{item.text}"
            for index, item in enumerate(subtitles, 1)
        )
        + "\n",
        encoding="utf-8",
    )
