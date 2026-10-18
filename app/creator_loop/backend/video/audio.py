"""Finite local audio normalization. No TTS service or cloud fallback."""

from __future__ import annotations

SILENCE_INPUT = ("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo")


def audio_filter(duration_ms: int) -> str:
    duration = duration_ms / 1000
    return (
        "aformat=sample_rates=48000:channel_layouts=stereo,"
        f"apad=whole_dur={duration:.6f},atrim=duration={duration:.6f},"
        "asetpts=PTS-STARTPTS"
    )


def audio_output_args(duration_ms: int) -> list[str]:
    return [
        "-af",
        audio_filter(duration_ms),
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        "48000",
    ]
