"""Fail closed while a confirmed restore still needs state/health recovery."""

from pathlib import Path

from creator_loop.windows_paths import file_io_path


class PendingRestore(RuntimeError):
    """A durable restore guard requires explicit state recovery."""


def require_no_pending_restore(root: Path) -> None:
    marker = root / "runtime/restore-in-progress.json"
    io = file_io_path(marker)
    if io.exists() or io.is_symlink() or io.is_junction():
        raise PendingRestore(
            "Pending DB restore requires explicit recovery before launch"
        )
