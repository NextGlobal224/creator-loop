"""Pinned, externally installed browser bytes; never an ordinary user browser."""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from creator_loop.runtime_files import RuntimeHandle
from creator_loop.windows_paths import file_io_path, resolve_file_path


def browser_pin() -> dict[str, Any]:
    return json.loads(
        (Path(__file__).parent / "resources" / "facebook-browser-pin.json").read_text(
            encoding="utf-8"
        )
    )


def verify_browser_namespace(
    directory: Path, *, pin: dict[str, Any] | None = None
) -> list[Path]:
    expected_files = set((browser_pin() if pin is None else pin)["files"])
    expected_directories = {
        parent.as_posix()
        for name in expected_files
        for parent in Path(name).parents
        if parent != Path(".")
    }
    entries = list(directory.rglob("*"))
    if any(child.is_symlink() or child.is_junction() for child in entries):
        raise ValueError("Linked browser namespace")
    files = {x.relative_to(directory).as_posix() for x in entries if x.is_file()}
    directories = {x.relative_to(directory).as_posix() for x in entries if x.is_dir()}
    if files != expected_files or directories != expected_directories:
        raise ValueError("Browser runtime namespace mismatch")
    return entries


def verified_browser(root: Path, held: ExitStack) -> Path:
    config = root / "manifests" / "facebook-browser-runtime.json"
    body = held.enter_context(RuntimeHandle(config, read_only=True)).read_json(
        reject_duplicates=True
    )
    if (
        set(body) != {"format", "path", "pin_sha256"}
        or type(body["format"]) is not int
        or body["format"] != 1
    ):
        raise ValueError("Invalid browser component selection")
    pin_path = Path(__file__).parent / "resources" / "facebook-browser-pin.json"
    if body["pin_sha256"] != hashlib.sha256(pin_path.read_bytes()).hexdigest():
        raise ValueError("Browser pin identity mismatch")
    directory = Path(body["path"])
    if (
        not directory.is_absolute()
        or resolve_file_path(directory, strict=True) != directory
    ):
        raise ValueError("Canonical external browser component required")
    held.enter_context(RuntimeHandle(directory, directory=True, read_only=True))
    pin = browser_pin()
    entries = verify_browser_namespace(directory)
    for child in entries:
        if child.is_symlink() or child.is_junction():
            raise ValueError("Linked browser namespace")
        if child.is_dir():
            held.enter_context(RuntimeHandle(child, directory=True, read_only=True))
    # Directories cannot be replaced; pinned files deny write/delete. Windows
    # directory leases do not prevent additions: verify the exact namespace
    # before launch and again after browser/driver cleanup before accepting.
    verify_browser_namespace(directory)
    for relative, expected in pin["files"].items():
        path = directory / relative
        if not path.is_relative_to(directory):
            raise ValueError("Invalid browser component containment")
        held.enter_context(RuntimeHandle(path, read_only=True))
        # RuntimeHandle checks regular, single-link, real canonical files and
        # retains deny-write/delete leases until the browser context closes.
        with file_io_path(path).open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if path.stat().st_size != expected["size"] or digest != expected["sha256"]:
            raise ValueError("Browser component digest mismatch")
    return directory / "chrome.exe"
