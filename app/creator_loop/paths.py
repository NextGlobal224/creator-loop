"""Resolve persistent user data separately from the replaceable installation."""

import os
from pathlib import Path


def data_root() -> Path:
    override = os.environ.get("CREATOR_LOOP_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise RuntimeError(
                "LOCALAPPDATA is unavailable; set CREATOR_LOOP_DATA_ROOT"
            )
        return Path(base) / "CreatorLoop"
    return Path.home() / ".local" / "share" / "CreatorLoop"


def ensure_data_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in (
        "storage/originals",
        "storage/derived",
        "models",
        "components",
        "manifests",
        "backups",
        "logs",
        "runtime",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
