"""Validate the V1 locator contract before writing Evidence."""

import hashlib
import json
import unicodedata


def validate_locator(
    kind: str,
    raw: str,
    *,
    duration_ms: int | None = None,
    text_length: int | None = None,
    text_snapshot: str | None = None,
) -> dict:
    if text_snapshot is not None:
        if not isinstance(text_snapshot, str):
            raise ValueError("Text snapshot must be a string")
        nfc_snapshot = unicodedata.normalize("NFC", text_snapshot)
        text_length = len(nfc_snapshot)
    else:
        nfc_snapshot = None

    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Locator data must be an object")
    if kind == "TIME_RANGE":
        if set(value) != {"start_ms", "end_ms", "track"}:
            raise ValueError("Invalid time range fields")
        start, end = value["start_ms"], value["end_ms"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError("Invalid time range")
        if duration_ms is not None and end > duration_ms:
            raise ValueError("Time range exceeds duration")
        if value["track"] not in ("audio", "video"):
            raise ValueError("Unknown track")
    elif kind == "IMAGE_REGION":
        if set(value) != {"x", "y", "width", "height"}:
            raise ValueError("Invalid image region fields")
        x, y, w, h = (value[k] for k in ("x", "y", "width", "height"))
        if any(type(v) not in (int, float) for v in (x, y, w, h)) or not (
            0 <= x < 1 and 0 <= y < 1 and w > 0 and h > 0 and x + w <= 1 and y + h <= 1
        ):
            raise ValueError("Invalid image region")
    elif kind == "TEXT_RANGE":
        if set(value) != {"start", "end", "text_digest"}:
            raise ValueError("Invalid text range fields")
        start, end = value["start"], value["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError("Invalid text range")
        if text_length is not None and end > text_length:
            raise ValueError("Text range exceeds snapshot")
        if not isinstance(value["text_digest"], str) or not value[
            "text_digest"
        ].startswith("sha256:"):
            raise ValueError("Text digest required")
        if nfc_snapshot is not None:
            expected_digest = (
                "sha256:" + hashlib.sha256(nfc_snapshot.encode("utf-8")).hexdigest()
            )
            if value["text_digest"] != expected_digest:
                raise ValueError("Text digest does not match NFC snapshot")
    elif kind == "WHOLE_ASSET":
        if value:
            raise ValueError("Whole asset locator must be empty")
    else:
        raise ValueError("Unknown locator type")
    return value
