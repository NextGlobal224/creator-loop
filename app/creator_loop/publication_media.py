"""Hold verified media bytes through the Package transaction on Windows."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

from creator_loop.originals import READ_CHUNK_SIZE
from creator_loop.storage_paths import resolve_storage_path
from creator_loop.windows_owned_file import _INVALID_HANDLE_VALUE, _kernel32


def _open_read_lock(path: Path) -> BinaryIO:
    if os.name != "nt":
        # Portable CI verifies bytes/DB invariants; Windows tests verify sharing.
        return path.open("rb")
    import ctypes
    import msvcrt

    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def hold_publication_media(
    db: sqlite3.Connection,
    *,
    file_id: str,
    expected_digest: str | None,
    data_root: Path,
    handles: ExitStack,
) -> str:
    """Fail closed on absent/conflicting rights; REUSE_MEDIA is only intent.

    All linked Source declarations must permit reuse. Conflicting or unknown
    declarations require a separate file assessment; this service grants none.
    The caller keeps handles open through commit and rechecks on approval/post.
    """
    row = db.execute(
        """SELECT f.asset_id,f.role,f.storage_key,f.sha256,f.byte_size,a.deleted_at
        FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id WHERE f.file_id=?""",
        (file_id,),
    ).fetchone()
    if row is None or row[5] is not None:
        raise ValueError(f"Media {file_id}: live registered file is required")
    asset_id, role, key, digest, size, _deleted = row
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or digest != expected_digest
        or type(size) is not int
        or size < 0
    ):
        raise ValueError(f"Media {file_id}: digest/size metadata mismatch")
    rights = db.execute(
        """SELECT DISTINCT s.source_id,s.rights_status FROM sources s
        JOIN source_assets sa ON sa.source_id=s.source_id WHERE sa.asset_id=?
        ORDER BY s.source_id""",
        (asset_id,),
    ).fetchall()
    if not rights or any(
        status not in ("OWNED", "LICENSED") for _source, status in rights
    ):
        detail = (
            "; ".join(f"{source}: {status}" for source, status in rights) or "UNKNOWN"
        )
        raise ValueError(f"Media {file_id}: reuse rights unresolved ({detail})")
    path = resolve_storage_path(data_root, role, key)
    try:
        stream = handles.enter_context(_open_read_lock(path))
        actual = hashlib.sha256()
        total = 0
        while chunk := stream.read(READ_CHUNK_SIZE):
            actual.update(chunk)
            total += len(chunk)
        if total != size or actual.hexdigest() != digest:
            raise ValueError(
                f"Media {file_id}: physical bytes do not match digest/size"
            )
    except OSError as exc:
        raise ValueError(f"Media {file_id}: file unavailable ({exc})") from exc
    return digest
