"""Session-safe browser failure codes; never retain exception messages."""

from __future__ import annotations

import re
from typing import Any


def browser_failure(error: Exception) -> dict[str, Any]:
    kind = type(error).__name__
    if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", kind) is None:
        kind = "UnknownError"
    status = (
        "TIMED_OUT"
        if isinstance(error, TimeoutError)
        or (kind == "TimeoutError" and type(error).__module__.startswith("playwright."))
        else "BROWSER_FAILED"
    )
    locations = []
    traceback = error.__traceback__
    while traceback is not None:
        module = traceback.tb_frame.f_globals.get("__name__", "")
        function = traceback.tb_frame.f_code.co_name
        if (
            isinstance(module, str)
            and module.startswith("creator_loop.backend.facebook.")
            and re.fullmatch(r"[A-Za-z_0-9.]{1,160}", module)
            and re.fullmatch(r"[A-Za-z_0-9]{1,80}", function)
        ):
            locations.append(f"{module}.{function}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    return {
        "status": status,
        "count": 0,
        "diagnostic": {"exception_type": kind, "locations": locations[-6:]},
    }


def diagnostic_label(value: Any) -> str:
    if not isinstance(value, dict) or set(value) != {"exception_type", "locations"}:
        return ""
    kind = value["exception_type"]
    locations = value["locations"]
    if (
        not isinstance(kind, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", kind) is None
        or not isinstance(locations, list)
        or len(locations) > 6
        or any(
            not isinstance(item, str)
            or re.fullmatch(
                r"creator_loop\.backend\.facebook\.[A-Za-z_0-9.]{1,240}:[1-9][0-9]{0,6}",
                item,
            )
            is None
            for item in locations
        )
    ):
        return ""
    return kind + (" @ " + locations[-1] if locations else "")
