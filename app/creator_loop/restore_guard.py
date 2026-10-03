"""Fail closed while a confirmed restore still needs state/health recovery."""

from pathlib import Path


class PendingRestore(RuntimeError):
    """A durable restore guard requires explicit state recovery."""


def require_no_pending_restore(root: Path) -> None:
    marker = root / "runtime/restore-in-progress.json"
    if marker.exists() or marker.is_symlink() or marker.is_junction():
        raise PendingRestore(
            "Pending DB restore requires explicit recovery before launch"
        )
