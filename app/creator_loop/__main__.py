"""Minimal Windows desktop launcher; --smoke validates packaged execution."""

import argparse
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from . import __version__
from .app_lock import AppDataLock, DataRootBusy
from .database import initialize, open_readonly, validate
from .paths import data_root, ensure_data_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--smoke", action="store_true", help="Create/open a DB and exit"
    )
    parser.add_argument(
        "--ui-smoke", action="store_true", help="Open the Library UI briefly and exit"
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Validate a DB-only backup with the app closed",
    )
    parser.add_argument(
        "--stage-update", type=Path, help="Verify and stage a supplied ZIP"
    )
    parser.add_argument(
        "--prepare-update",
        type=Path,
        help="Backup, stage and migrate; activation pending",
    )
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--installation-root", type=Path)
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Read-only schema/storage health; no migration or UI",
    )
    args = parser.parse_args()
    if args.backup and (args.smoke or args.ui_smoke):
        parser.error("--backup cannot be combined with smoke modes")
    root = data_root()
    if args.health_check:
        if (
            args.backup
            or args.smoke
            or args.ui_smoke
            or args.stage_update
            or args.prepare_update
            or args.release_manifest is not None
            or args.installation_root is not None
        ):
            parser.error("--health-check cannot be combined with other modes")
        from .update_health import readonly_health

        try:
            health_result = readonly_health(root)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"Health refused: {type(exc).__name__}", file=sys.stderr)
            return 4
        print(json.dumps(health_result, sort_keys=True))
        return 0
    if args.stage_update and args.prepare_update:
        parser.error("Choose stage-only or update preparation")
    if args.stage_update or args.prepare_update:
        if (
            args.backup
            or args.smoke
            or args.ui_smoke
            or args.release_manifest is None
            or args.installation_root is None
        ):
            parser.error(
                "Staging requires --release-manifest and --installation-root, without other modes"
            )
        from .installation_stage import stage_installation
        from .update_preparation import prepare_update

        try:
            if args.prepare_update:
                candidate = prepare_update(
                    root,
                    args.prepare_update,
                    args.release_manifest,
                    args.installation_root,
                )
            else:
                candidate = stage_installation(
                    args.stage_update,
                    args.release_manifest,
                    args.installation_root,
                    user_data_root=root,
                )
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            zipfile.BadZipFile,
        ) as exc:
            print(f"Update/staging refused: {exc}", file=sys.stderr)
            return 4
        result_type = (
            "Prepared update journal"
            if args.prepare_update
            else "Verified installation staged"
        )
        print(f"{result_type}: {candidate.name}; activation pending")
        return 0
    if args.release_manifest is not None or args.installation_root is not None:
        parser.error("Staging paths require --stage-update")
    if args.backup:
        from .update_backup import create_update_backup

        try:
            result = create_update_backup(root)
        except DataRootBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            print(f"Backup refused: {exc}", file=sys.stderr)
            return 4
        print(f"Validated DB-only backup: {result.name}; media is not included")
        return 0
    ensure_data_root(root)
    try:
        with AppDataLock(root):
            return _run(args, root)
    except DataRootBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3


def _run(args: argparse.Namespace, root: Path) -> int:
    """Initialize and run only while the app/updater coordination lock is held."""
    db_path = root / "creator_loop.sqlite3"
    initialize(db_path)
    with closing(open_readonly(db_path)) as db:
        validate(db)
    if args.smoke:
        print(f"Creator Loop {__version__}: schema OK at {db_path}")
        return 0
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 is required for the desktop UI", file=sys.stderr)
        return 2
    from .library_ui import LibraryWindow

    app = QApplication(sys.argv)
    window = LibraryWindow(root)
    window.show()
    if args.ui_smoke:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(200, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
