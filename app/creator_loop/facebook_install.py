"""Install a pinned browser directly from its official source, outside app ZIP."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.request
import zipfile
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from creator_loop.facebook_runtime import browser_pin, verify_browser_namespace
from creator_loop.paths import ensure_data_root
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.windows_paths import file_io_path, resolve_file_path

MAX_DOWNLOAD = 300 * 1024**2


def _install_browser(root: Path, held: ExitStack) -> Path:
    ensure_data_root(root)
    root = resolve_file_path(root, strict=True)
    for folder in (root, root / "components", root / "manifests"):
        held.enter_context(
            RuntimeHandle(folder, directory=True, allow_child_writes=True)
        )
    pin = browser_pin()
    target = root / "components" / ("facebook-browser-" + pin["browser_version"])
    if (root / "components").is_symlink() or (root / "components").is_junction():
        raise ValueError("Real component directory required")

    def verify(directory: Path) -> None:
        if (
            resolve_file_path(directory, strict=True) != directory
            or directory.is_symlink()
            or directory.is_junction()
        ):
            raise ValueError("Real canonical browser directory required")
        verify_browser_namespace(directory, pin=pin)
        for name, expected in pin["files"].items():
            path = directory / name
            if path.is_symlink() or path.is_junction() or path.stat().st_nlink != 1:
                raise ValueError("Linked browser component")
            with file_io_path(path).open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if path.stat().st_size != expected["size"] or digest != expected["sha256"]:
                raise ValueError("Browser component hash mismatch")

    if target.exists():
        verify(target)
    else:
        deadline = time.monotonic() + 600
        with tempfile.TemporaryDirectory(
            prefix="browser-install-", dir=root / "components"
        ) as work:
            stage = Path(work)
            archive = stage / "upstream.zip"
            expanded = stage / "expanded"
            expanded.mkdir()
            total = 0
            with (
                urllib.request.urlopen(pin["download_url"], timeout=30) as response,
                archive.open("xb") as downloaded,
            ):
                # The source is fixed HTTPS; redirects may stay only on Google's distribution host.
                if not response.url.startswith(
                    "https://storage.googleapis.com/chrome-for-testing-public/"
                ):
                    raise ValueError("Unexpected browser download source")
                while chunk := response.read(256 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD or time.monotonic() > deadline:
                        raise TimeoutError("Browser download budget exceeded")
                    downloaded.write(chunk)
            with zipfile.ZipFile(archive) as package:
                members = [m for m in package.infolist() if not m.is_dir()]
                if len(members) != len(pin["files"]) or {
                    m.filename for m in members
                } != {"chrome-win64/" + n for n in pin["files"]}:
                    raise ValueError("Browser archive inventory mismatch")
                for member in members:
                    name = member.filename.removeprefix("chrome-win64/")
                    expected = pin["files"][name]
                    if (
                        member.file_size != expected["size"]
                        or member.file_size > 400 * 1024**2
                    ):
                        raise ValueError("Browser archive member budget mismatch")
                    destination = expanded / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with package.open(member) as source, destination.open("xb") as sink:
                        while chunk := source.read(256 * 1024):
                            if time.monotonic() > deadline:
                                raise TimeoutError(
                                    "Browser installation budget exceeded"
                                )
                            digest.update(chunk)
                            sink.write(chunk)
                    if digest.hexdigest() != expected["sha256"]:
                        raise ValueError("Browser archive member digest mismatch")
            verify(expanded)
            # New version target only. Never replace/discard an existing component.
            expanded.rename(target)
    pin_path = Path(__file__).parent / "resources" / "facebook-browser-pin.json"
    config = root / "manifests" / "facebook-browser-runtime.json"
    temp = config.with_name("facebook-browser-runtime-" + uuid4().hex + ".tmp")
    with file_io_path(temp).open("x", encoding="utf-8") as config_output:
        json.dump(
            {
                "format": 1,
                "path": str(target),
                "pin_sha256": hashlib.sha256(pin_path.read_bytes()).hexdigest(),
            },
            config_output,
        )
        config_output.flush()
        os.fsync(config_output.fileno())
    os.replace(file_io_path(temp), file_io_path(config))
    return target


def install_browser(root: Path) -> Path:
    with ExitStack() as held:
        return _install_browser(root, held)
