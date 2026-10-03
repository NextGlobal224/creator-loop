"""Minimal Windows desktop launcher; --smoke validates packaged execution."""

import argparse
import sqlite3
import sys
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
    args = parser.parse_args()
    if args.backup and (args.smoke or args.ui_smoke):
        parser.error("--backup cannot be combined with smoke modes")
    root = data_root()
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
