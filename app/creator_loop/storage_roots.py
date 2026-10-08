"""Registered media roots are coordination metadata, never domain identities."""

from __future__ import annotations

import json
import os
import re
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from creator_loop.database import _connect_write
from creator_loop.storage_volumes import volume_identity
from creator_loop.transactions import atomic_transaction
from creator_loop.windows_paths import file_io_path, resolve_file_path


class StorageRootError(ValueError):
    pass


@dataclass(frozen=True)
class StorageRoot:
    root_id: str
    path: str
    volume_id: str


def _manifest_path(data_root: Path) -> Path:
    canonical = resolve_file_path(Path(data_root), strict=True)
    manifests = canonical / "manifests"
    if not resolve_file_path(manifests).is_relative_to(canonical):
        raise StorageRootError("Unsafe storage manifest folder")
    path = manifests / "storage-roots.json"
    if file_io_path(path).is_symlink():
        raise StorageRootError("Unsafe storage manifest file")
    return path


def _load(path: Path) -> dict[str, object]:
    try:
        with file_io_path(path).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise StorageRootError("Storage manifest exceeds metadata budget")
        payload = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("manifest_version") != 1
            or not isinstance(payload.get("data_root_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["data_root_id"]) is None
            or type(payload.get("schema_version")) is not int
            or not isinstance(payload.get("storage_roots"), list)
        ):
            raise StorageRootError("Invalid storage manifest")
        ids: set[str] = set()
        paths: set[str] = set()
        for entry in payload["storage_roots"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"root_id", "path", "volume_id"}
                or any(
                    not isinstance(value, str) or not value for value in entry.values()
                )
                or re.fullmatch(r"[0-9a-f]{32}", entry["root_id"]) is None
                or not Path(entry["path"]).is_absolute()
                or entry["root_id"] in ids
                or os.path.normcase(entry["path"]) in paths
            ):
                raise StorageRootError("Invalid or duplicate storage root")
            ids.add(entry["root_id"])
            paths.add(os.path.normcase(entry["path"]))
        default = payload.get("default_storage_root_id")
        if default is not None and (not isinstance(default, str) or default not in ids):
            raise StorageRootError("Invalid default storage root")
        return payload
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRootError("Storage manifest unavailable or malformed") from exc


def _roots(payload: dict[str, object]) -> list[StorageRoot]:
    # _load validates all fields before this conversion.
    entries = payload["storage_roots"]
    if not isinstance(entries, list):
        raise StorageRootError("Invalid storage roots")
    return [StorageRoot(**entry) for entry in entries]


def list_storage_roots(data_root: Path) -> list[StorageRoot]:
    """List registration history even while a media volume is unavailable."""
    path = _manifest_path(data_root)
    if not file_io_path(path).exists():
        return []
    return _roots(_load(path))


def default_storage_root_id(data_root: Path) -> str | None:
    """Read the preference without silently resolving an unavailable root."""
    path = _manifest_path(data_root)
    if not file_io_path(path).exists():
        return None
    default = _load(path).get("default_storage_root_id")
    return default if isinstance(default, str) else None


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    file_io_path(path.parent).mkdir(exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    created = False
    try:
        with file_io_path(temporary).open("x", encoding="utf-8") as stream:
            created = True
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(file_io_path(temporary), file_io_path(path))
    finally:
        if created:
            file_io_path(temporary).unlink(missing_ok=True)


def set_default_storage_root(data_root: Path, root_id: str | None) -> None:
    """Choose future destinations; existing file keys and registrations stay intact."""
    canonical = Path(data_root).resolve(strict=True)
    db_path = canonical / "creator_loop.sqlite3"
    if not db_path.is_file():
        raise StorageRootError("User database must exist")
    with closing(_connect_write(db_path)) as db, atomic_transaction(db):
        path = _manifest_path(canonical)
        if not path.exists() and root_id is None:
            return
        payload = _load(path)
        if root_id is not None:
            resolve_registered_root(canonical, root_id)
        payload["default_storage_root_id"] = root_id
        payload["schema_version"] = db.execute("PRAGMA user_version").fetchone()[0]
        _write_manifest(path, payload)


def resolve_registered_root(data_root: Path, root_id: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{32}", root_id) is None:
        raise StorageRootError("Invalid registered root ID")
    root = next(
        (root for root in list_storage_roots(data_root) if root.root_id == root_id),
        None,
    )
    if root is None:
        raise StorageRootError("Storage root is not registered")
    try:
        path = Path(root.path).resolve(strict=True)
        if not path.is_dir() or str(path) != root.path:
            raise StorageRootError("Registered root path has changed")
        if volume_identity(path) != root.volume_id:
            raise StorageRootError("Registered storage volume has changed")
    except OSError as exc:
        raise StorageRootError("Registered storage volume unavailable") from exc
    return path


def register_storage_root(data_root: Path, media_root: Path) -> StorageRoot:
    """Register an existing local folder without moving files or granting deletion.

    Serialize manifest writers with the existing user DB's write lock, then
    replace the manifest atomically. The manifest is allowed to outlive a DB
    rollback: registration alone changes no Asset/file/Evidence relation.
    """
    canonical = Path(data_root).resolve(strict=True)
    candidate = Path(media_root).resolve(strict=True)
    if not candidate.is_dir():
        raise StorageRootError("Storage root must be an existing directory")
    if candidate.is_relative_to(canonical) or canonical.is_relative_to(candidate):
        raise StorageRootError(
            "Registered storage root must be separate from user data"
        )
    observed_volume = volume_identity(candidate)
    db_path = canonical / "creator_loop.sqlite3"
    if not db_path.is_file():
        raise StorageRootError("User database must exist before registration")
    with closing(_connect_write(db_path)) as db, atomic_transaction(db):
        path = _manifest_path(canonical)
        payload = (
            _load(path)
            if path.exists()
            else {
                "manifest_version": 1,
                "data_root_id": uuid4().hex,
                "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
                "storage_roots": [],
            }
        )
        roots = _roots(payload)
        for root in roots:
            if os.path.normcase(root.path) == os.path.normcase(str(candidate)):
                resolve_registered_root(canonical, root.root_id)
                return root
            registered = Path(root.path)
            if candidate.is_relative_to(registered) or registered.is_relative_to(
                candidate
            ):
                raise StorageRootError("Registered storage roots must not overlap")
        if volume_identity(candidate) != observed_volume:
            raise StorageRootError("Storage volume changed during registration")
        root = StorageRoot(uuid4().hex, str(candidate), observed_volume)
        payload["storage_roots"] = [asdict(item) for item in [*roots, root]]
        payload["schema_version"] = db.execute("PRAGMA user_version").fetchone()[0]
        _write_manifest(path, payload)
    return root
