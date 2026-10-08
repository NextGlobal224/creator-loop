"""Read-only DB/storage health and a bounded Windows owned-process launcher."""

from __future__ import annotations

import importlib
import json
import os
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from creator_loop.database import SCHEMA_VERSION, open_readonly, validate
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.storage_paths import resolve_storage_path
from creator_loop.windows_paths import file_io_path


def readonly_health(root: Path) -> dict[str, object]:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        # Database queries alone must not bless a packaged app with broken DLLs.
        # Import lightweight bundled dependencies; no window, model or engine.
        try:
            for module in (
                "PySide6.QtCore",
                "PySide6.QtGui",
                "PySide6.QtWidgets",
                "PySide6.QtMultimedia",
                "PySide6.QtMultimediaWidgets",
            ):
                importlib.import_module(module)
        except (ImportError, OSError) as exc:
            raise RuntimeError("Bundled Qt runtime is unavailable") from exc
        core = importlib.import_module("PySide6.QtCore")
        platforms = Path(getattr(sys, "_MEIPASS")) / "PySide6/plugins/platforms"
        # Qt may import successfully while its platform DLLs fail to load.
        # Explicit owned paths prevent an ambient plugin from masking a broken
        # bundle. Loading the factories creates no window/model/engine.
        for name in ("qwindows.dll", "qoffscreen.dll"):
            plugin = core.QPluginLoader(str(platforms / name))
            if not plugin.load():
                raise RuntimeError("Bundled Qt platform plugin is unavailable")
    canonical = root.resolve(strict=True)
    source = canonical / "creator_loop.sqlite3"
    if not source.is_file() or source.is_symlink() or source.is_junction():
        raise OSError("A real existing database is required for health")
    with closing(open_readonly(source)) as db:
        validate(db)
        db.execute("SELECT count(*) FROM assets").fetchone()
        if db.execute(
            "SELECT 1 FROM processing_runs WHERE status IN ('QUEUED','RUNNING') LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Active processing requires recovery before update health"
            )
        reference = db.execute(
            "SELECT role,storage_key,byte_size FROM asset_files ORDER BY file_id LIMIT 1"
        ).fetchone()
        if reference is not None:
            role, key, size = reference
            path = resolve_storage_path(canonical, role, key)
            if not path.is_file() or path.stat().st_size != size:
                raise RuntimeError(
                    "Sample storage reference unavailable or size mismatch"
                )
    return {
        "health_format": 1,
        "schema_version": SCHEMA_VERSION,
        "query_ok": True,
        "storage_reference": "resolved" if reference is not None else "not_applicable",
    }


def run_health_check(
    executable: Path,
    root: Path,
    log_directory: Path,
    *,
    component_version: str,
    timeout_seconds: float = 60,
    arguments_prefix: list[str] | None = None,
) -> dict[str, Any]:
    """Caller verifies the candidate and holds the update's app/DB writer locks.

    The child only runs --health-check. A private native Job owns the launcher
    and any lightweight storage-probe descendants, including on owner crash.
    No model or external engine is launched by this mode.
    """
    environment = os.environ.copy()
    environment["CREATOR_LOOP_DATA_ROOT"] = str(root.resolve(strict=True))
    environment["PYTHONIOENCODING"] = "utf-8"
    with OwnedWindowsProcess(
        file_io_path(executable),
        [*(arguments_prefix or []), "--health-check"],
        log_directory,
        component_version=component_version,
        environment=environment,
    ) as process:
        outcome = process.wait(timeout_seconds)
        record = {
            "health_run_format": 1,
            "ownership": process.record,
            **asdict(outcome),
        }
    file_io_path(log_directory / "result.json").write_text(
        json.dumps(record, sort_keys=True, indent=2), encoding="utf-8"
    )
    if outcome.timed_out:
        raise TimeoutError("Candidate health timed out; owned tree stopped")
    if outcome.exit_code != 0:
        raise RuntimeError("Candidate health failed; inspect its bounded logs")
    with file_io_path(process.stdout_path).open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Candidate health output exceeds metadata budget")
    payload = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("health_format") != 1
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("query_ok") is not True
        or payload.get("storage_reference") not in ("resolved", "not_applicable")
    ):
        raise RuntimeError("Candidate health result is invalid")
    return payload
