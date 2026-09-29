"""Minimal Windows desktop launcher; --smoke validates packaged execution."""

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from . import __version__
from .database import initialize, open_readonly, validate
from .paths import data_root, ensure_data_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--smoke", action="store_true", help="Create/open a DB and exit"
    )
    args = parser.parse_args()
    root = data_root()
    ensure_data_root(root)
    db_path = root / "creator_loop.sqlite3"
    initialize(db_path)
    with open_readonly(db_path) as db:
        validate(db)
    if args.smoke:
        print(f"Creator Loop {__version__}: schema OK at {db_path}")
        return 0
    try:
        from PySide6.QtWidgets import QApplication, QLabel
    except ImportError:
        print("PySide6 is required for the desktop UI", file=sys.stderr)
        return 2
    app = QApplication(sys.argv)
    label = QLabel(f"Creator Loop {__version__}\nSchema V1 ready")
    label.setWindowTitle("Creator Loop")
    label.resize(360, 140)
    label.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
