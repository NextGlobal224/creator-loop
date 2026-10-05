"""Strict, held fresh-copy history; phases never establish successful recovery."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO

from creator_loop.corrupt_database import _NAMES, _regular
from creator_loop.corrupt_restore_copy import _hash
from creator_loop.runtime_files import RuntimeHandle


def hold_fresh_history(
    root: Path,
    copy: dict[str, Any],
    copy_sha: str,
    leases: ExitStack,
    acquire: Callable[[Path], BinaryIO | None],
    read_record: Callable[[Path], dict[str, Any]],
    check: Callable[[], None],
    *,
    ignored_journal_name: str | None = None,
    anchored_journals: list[str] | None = None,
) -> tuple[list[dict[str, Any]], set[tuple[str, int]], Callable[[], None]]:
    """Locate retained bytes by native identity across explicitly bound retries.

    A pending journal may still name live bytes or bytes in a later fresh
    retention. Never select a receipt by timestamp, replay a sidecar, repair a
    missing archive, or interpret current raw bytes as a consistent backup.
    """
    prefix = f"corrupt-fresh-restore-{copy['copy_id']}-"
    manifests = root / "manifests"

    def names() -> set[str]:
        return {
            path.name
            for path in manifests.iterdir()
            if path.name.startswith(prefix) and path.name != ignored_journal_name
        }

    inventory = names()
    anchored = set(anchored_journals or [])
    if not anchored <= inventory:
        raise ValueError("Anchored fresh journal missing; keep every guard/archive")
    if len(inventory) > 64:
        raise ValueError("Bounded fresh history required; retain all history")
    records: list[tuple[Path, dict[str, Any]]] = []
    directories: dict[Path, set[str]] = {}
    absent_directories: set[Path] = set()
    candidates: dict[tuple[str, int, int], list[tuple[Path, BinaryIO]]] = {}

    def add_file(path: Path) -> None:
        stream = acquire(path)
        if stream is not None:
            stat = os.fstat(stream.fileno())
            candidates.setdefault((path.name, stat.st_dev, stat.st_ino), []).append(
                (path, stream)
            )

    for name in sorted(inventory):
        check()
        match = re.fullmatch(re.escape(prefix) + r"([0-9a-f]{32})\.json", name)
        path = manifests / name
        stat = _regular(path)
        if match is None or stat is None or not 0 < stat.st_size <= 16384:
            raise ValueError("Canonical bounded fresh journal required")
        record = read_record(path)
        expected = {
            "corrupt_resume_format": 1,
            "fresh_restore_format": 1,
            "resume_id": match[1],
            "copy_id": copy["copy_id"],
            "copy_journal_name": f"corrupt-restore-{copy['copy_id']}.json",
            "copy_journal_sha256": copy_sha,
            "data_root_identity": copy["data_root_identity"],
            "preparation_sha256": copy["preparation_sha256"],
            "selected_backup_id": copy["backup_id"],
            "selected_candidate_directory": copy["candidate_directory"],
            "confirmed_fresh_restore": True,
            "confirmed_lost_changes": True,
            "confirmed_keep_partial": False,
            "confirmed_preserve_unknown": False,
            "consistent_partial_backup": False,
            "consistent_current_backup": False,
            "activation_pending": True,
            "restored": False,
        }
        if any(
            type(record.get(key)) is not type(value) or record[key] != value
            for key, value in expected.items()
        ):
            raise ValueError("Fresh journal root/copy/choice/consent mismatch")
        for key in ("reviewed_fresh_restore", "reviewed_inspection"):
            if (
                not isinstance(record.get(key), str)
                or re.fullmatch(r"[0-9a-f]{64}", record[key]) is None
            ):
                raise ValueError("Fresh reviewed identity required")
        entries = record.get("current_source_files")
        if not isinstance(entries, list) or len(entries) != len(_NAMES):
            raise ValueError("Complete fresh current bundle required")
        present: list[dict[str, Any]] = []
        for entry, source_name in zip(entries, _NAMES, strict=True):
            if (
                not isinstance(entry, dict)
                or set(entry)
                != {"name", "present", "sha256", "byte_size", "device", "inode"}
                or entry["name"] != source_name
                or type(entry["present"]) is not bool
            ):
                raise ValueError("Typed fresh current inventory required")
            if entry["present"]:
                if (
                    not isinstance(entry["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                    or any(
                        type(entry[key]) is not int
                        for key in ("byte_size", "device", "inode")
                    )
                    or entry["byte_size"] < 0
                    or entry["device"] < 0
                    or entry["inode"] <= 0
                ):
                    raise ValueError("Fresh digest/size/native identity required")
                present.append(entry)
            elif any(
                entry[key] is not None
                for key in ("sha256", "byte_size", "device", "inode")
            ):
                raise ValueError("Absent fresh source must have null proof")
        retained_entries = record.get("retained_partial_files")
        if (
            not isinstance(retained_entries, list)
            or len(retained_entries) != len(present)
            or any(not isinstance(entry, dict) for entry in retained_entries)
            or any(
                set(entry)
                != {"name", "present", "sha256", "byte_size", "device", "inode"}
                or not isinstance(entry.get("name"), str)
                or entry["name"] not in _NAMES
                or type(entry.get("present")) is not bool
                or entry["present"] is not True
                or not isinstance(entry.get("sha256"), str)
                or any(
                    type(entry.get(key)) is not int
                    for key in ("byte_size", "device", "inode")
                )
                for entry in retained_entries
            )
            or {entry["name"]: entry for entry in present}
            != {entry.get("name"): entry for entry in retained_entries}
        ):
            raise ValueError("Fresh retained inventory differs from selected bundle")
        folder_name = f"corrupt-original-{match[1]}" if present else None
        if record.get("retained_partial_directory") != folder_name:
            raise ValueError("Explicit fresh retention namespace required")
        if folder_name is not None:
            folder = root / "backups" / folder_name
            if folder.is_symlink() or folder.is_junction():
                raise ValueError("Linked fresh retention refused")
            if folder.exists():
                if (
                    folder.is_symlink()
                    or folder.is_junction()
                    or not folder.is_dir()
                    or folder.resolve(strict=True) != folder
                ):
                    raise ValueError("Real canonical fresh retention required")
                if sys.platform == "win32":
                    leases.enter_context(
                        RuntimeHandle(folder, directory=True, allow_child_writes=True)
                    )
                children = {child.name for child in folder.iterdir()}
                if children - {entry["name"] for entry in present}:
                    raise ValueError("Foreign fresh retention files")
                directories[folder] = children
                for child in sorted(children):
                    add_file(folder / child)
            else:
                absent_directories.add(folder)
        records.append((path, record))
    if records:
        for source_name in _NAMES:
            add_file(root / source_name)
    evidence: list[dict[str, Any]] = []
    completed: set[tuple[str, int]] = set()
    for path, record in records:
        located = []
        for entry in record["retained_partial_files"]:
            found = candidates.get((entry["name"], entry["device"], entry["inode"]), [])
            if len(found) != 1 or _hash(found[0][1], check) != (
                entry["sha256"],
                entry["byte_size"],
            ):
                raise ValueError(
                    "Fresh original missing, duplicate or changed; keep guard"
                )
            location = found[0][0]
            located.append(
                {
                    "name": entry["name"],
                    "location": location.relative_to(root).as_posix(),
                }
            )
        receipt_sha = record.get("copied_database_sha256")
        receipt_size = record.get("copied_database_byte_size")
        if path.name not in anchored and (
            receipt_sha is not None
            or receipt_size is not None
            or any(item["location"] != item["name"] for item in located)
        ):
            raise ValueError("Unanchored fresh journal already mutated current bytes")
        if receipt_sha is not None or receipt_size is not None:
            if (
                not isinstance(receipt_sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt_sha) is None
                or type(receipt_size) is not int
                or receipt_size <= 0
                or any(item["location"] == item["name"] for item in located)
            ):
                raise ValueError(
                    "Fresh physical receipt needs all current originals retained"
                )
            completed.add((receipt_sha, receipt_size))
        stream = acquire(path)
        assert stream is not None
        evidence.append(
            {
                "journal_name": path.name,
                "sha256": _hash(stream, check)[0],
                "retained_locations": located,
            }
        )

    def recheck() -> None:
        check()
        if names() != inventory or any(
            {path.name for path in folder.iterdir()} != children
            for folder, children in directories.items()
        ):
            raise RuntimeError("Fresh history inventory changed during inspection")
        if any(folder.exists() for folder in absent_directories):
            raise RuntimeError("Previously absent fresh retention appeared")

    return evidence, completed, recheck
