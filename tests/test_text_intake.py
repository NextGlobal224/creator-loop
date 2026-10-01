from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import patch

from creator_loop.database import initialize
from creator_loop.library import LibraryRepository
from creator_loop.originals import verify_original_file
from creator_loop.text_intake import intake_text_original


class _NonWindowsOwnedFile:
    """Test-only stand-in so non-Windows CI can exercise registration."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = path.open("x+b")
        self.delete_on_close = False

    @classmethod
    def create_new(cls, path: Path) -> _NonWindowsOwnedFile:
        return cls(path)

    def discard(self) -> None:
        self.delete_on_close = True

    def close(self) -> None:
        self.stream.close()
        if self.delete_on_close:
            self.path.unlink()


class _SimulatedWriteStream:
    def __init__(self, stream: BinaryIO, write_limits: list[int]) -> None:
        self._stream = stream
        self._write_limits = iter(write_limits)
        self.write_calls = 0

    def write(self, data: memoryview) -> int:
        self.write_calls += 1
        limit = next(self._write_limits, len(data))
        if limit <= 0:
            return limit
        return self._stream.write(data[:limit])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class TextOriginalIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "dữ liệu có dấu và khoảng trắng"
        self.root.mkdir()
        self.db_path = self.root / "creator_loop.sqlite3"
        initialize(self.db_path)
        self.source = self.base / "nguồn có dấu" / "Bản gốc Đà Nẵng.txt"
        self.source.parent.mkdir()
        self.original_bytes = b"\xef\xbb\xbf" + "Cafe\u0301 Đà Nẵng\r\n".encode("utf-8")
        self.source.write_bytes(self.original_bytes)

    def _counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.db_path)) as db:
            assets = db.execute("SELECT count(*) FROM assets").fetchone()[0]
            files = db.execute("SELECT count(*) FROM asset_files").fetchone()[0]
        return assets, files

    def _destination(self, file_id: str = "file-fixed") -> Path:
        return self.root / "storage" / "originals" / f"{file_id}-{self.source.name}"

    def test_success_preserves_exact_bytes_and_registers_actual_digest_and_size(
        self,
    ) -> None:
        result = intake_text_original(self.source, root=self.root)
        destination = self.root.joinpath(*result.storage_key.split("/"))

        self.assertEqual(destination.read_bytes(), self.original_bytes)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertIn(self.source.name, result.storage_key)
        self.assertNotEqual(result.asset_id, str(self.source))
        self.assertNotIn(str(self.source.parent), result.asset_id)

        with closing(sqlite3.connect(self.db_path)) as db:
            asset = db.execute(
                "SELECT media_type, display_name FROM assets WHERE asset_id=?",
                (result.asset_id,),
            ).fetchone()
            original = db.execute(
                """
                SELECT asset_id, role, storage_key, sha256, byte_size, mime_type,
                       parent_file_id, processing_run_id
                FROM asset_files WHERE file_id=?
                """,
                (result.file_id,),
            ).fetchone()
            self.assertEqual(asset, ("TEXT", self.source.name))
            self.assertEqual(
                original,
                (
                    result.asset_id,
                    "ORIGINAL",
                    result.storage_key,
                    hashlib.sha256(destination.read_bytes()).hexdigest(),
                    len(destination.read_bytes()),
                    "text/plain",
                    None,
                    None,
                ),
            )
            self.assertIsNone(verify_original_file(db, result.file_id, self.root))
        with destination.open("r+b") as reopened:
            self.assertEqual(reopened.read(), self.original_bytes)

    def test_existing_destination_is_never_overwritten_or_deleted(self) -> None:
        destination = self._destination()
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"pre-existing original")

        with patch(
            "creator_loop.text_intake._new_id",
            side_effect=("asset-fixed", "file-fixed"),
        ):
            with self.assertRaises(FileExistsError):
                intake_text_original(self.source, root=self.root)

        self.assertEqual(destination.read_bytes(), b"pre-existing original")
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(self._counts(), (0, 0))

    def test_short_write_completes_exact_original(self) -> None:
        import creator_loop.text_intake as text_intake

        owned_type = text_intake.OwnedWindowsFile
        create_new = owned_type.create_new
        streams: list[_SimulatedWriteStream] = []

        def create_with_short_write(path: Path) -> Any:
            owned = create_new(path)
            simulated = _SimulatedWriteStream(owned.stream, [2])
            owned.stream = simulated
            streams.append(simulated)
            return owned

        with patch.object(
            owned_type, "create_new", side_effect=create_with_short_write
        ):
            result = intake_text_original(self.source, root=self.root)

        destination = self.root.joinpath(*result.storage_key.split("/"))
        self.assertEqual(streams[0].write_calls, 2)
        self.assertEqual(destination.read_bytes(), self.original_bytes)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertIsNone(verify_original_file(db, result.file_id, self.root))

    def test_stalled_short_write_fails_without_registration(self) -> None:
        import creator_loop.text_intake as text_intake

        owned_type = text_intake.OwnedWindowsFile
        create_new = owned_type.create_new
        streams: list[_SimulatedWriteStream] = []

        def create_with_stalled_write(path: Path) -> Any:
            owned = create_new(path)
            simulated = _SimulatedWriteStream(owned.stream, [2, 0])
            owned.stream = simulated
            streams.append(simulated)
            return owned

        with (
            patch.object(
                owned_type, "create_new", side_effect=create_with_stalled_write
            ),
            patch(
                "creator_loop.text_intake._new_id",
                side_effect=("asset-fixed", "file-fixed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "no forward progress"):
                intake_text_original(self.source, root=self.root)

        self.assertEqual(streams[0].write_calls, 2)
        self.assertFalse(self._destination().exists())
        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    def test_physical_write_failure_leaves_no_database_record(self) -> None:
        with (
            patch(
                "creator_loop.text_intake._new_id",
                side_effect=("asset-fixed", "file-fixed"),
            ),
            patch(
                "creator_loop.text_intake.os.fsync", side_effect=OSError("disk full")
            ),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                intake_text_original(self.source, root=self.root)

        self.assertFalse(self._destination().exists())
        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    def test_database_registration_failure_removes_only_owned_destination(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO assets(asset_id,media_type,display_name,created_at) VALUES (?,?,?,?)",
                ("old-asset", "TEXT", "Earlier asset", "2026-10-01T00:00:00Z"),
            )
            db.execute(
                """
                INSERT INTO asset_files(
                    file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    "file-fixed",
                    "old-asset",
                    "ORIGINAL",
                    "storage/originals/old.txt",
                    "a" * 64,
                    1,
                    "text/plain",
                    "2026-10-01T00:00:00Z",
                ),
            )
            db.commit()

        with patch(
            "creator_loop.text_intake._new_id",
            side_effect=("asset-fixed", "file-fixed"),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                intake_text_original(self.source, root=self.root)

        self.assertFalse(self._destination().exists())
        with self._destination().open("xb") as reopened:
            reopened.write(b"new owner")
        self.assertEqual(self._counts(), (1, 1))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM assets WHERE asset_id='asset-fixed'"
                ).fetchone()
            )
            self.assertEqual(
                db.execute(
                    "SELECT storage_key FROM asset_files WHERE file_id='file-fixed'"
                ).fetchone(),
                ("storage/originals/old.txt",),
            )
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    @unittest.skipUnless(os.name == "nt", "requires Windows sharing semantics")
    def test_second_process_cannot_rename_or_replace_owned_destination(self) -> None:
        from creator_loop.windows_owned_file import OwnedWindowsFile

        destination = self._destination()
        moved_original = self.root / "storage" / "originals" / "moved-original.txt"
        replacement = self.root / "storage" / "originals" / "replacement.txt"
        replacement.parent.mkdir(parents=True)
        replacement.write_bytes(b"replacement owned by someone else")
        attempts: dict[str, dict[str, object]] = {}
        owned: OwnedWindowsFile | None = None
        create_new = OwnedWindowsFile.create_new

        def capture_owned(path: Path) -> OwnedWindowsFile:
            nonlocal owned
            owned = create_new(path)
            return owned

        def attempt_and_fail(_repo: LibraryRepository, **_kwargs: object) -> None:
            code = (
                "import json, os, sys\n"
                "target, moved, replacement = sys.argv[1:]\n"
                "result = {}\n"
                'for name, action in (("rename", lambda: os.rename(target, moved)), '
                '("replace", lambda: os.replace(replacement, target))):\n'
                "    try:\n"
                "        action()\n"
                '        result[name] = {"status": "succeeded"}\n'
                "    except OSError as exc:\n"
                '        result[name] = {"status": "denied", '
                '"winerror": exc.winerror}\n'
                "print(json.dumps(result))\n"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(destination),
                    str(moved_original),
                    str(replacement),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            attempts.update(json.loads(completed.stdout))
            self.assertEqual(set(attempts), {"rename", "replace"})
            for action in ("rename", "replace"):
                self.assertEqual(attempts[action]["status"], "denied", attempts)
                self.assertIsInstance(attempts[action]["winerror"], int)
            self.assertIsNotNone(owned)
            assert owned is not None
            self.assertTrue(destination.is_file())
            named = destination.stat()
            held = os.fstat(owned.stream.fileno())
            self.assertEqual((named.st_dev, named.st_ino), (held.st_dev, held.st_ino))
            owned.stream.seek(0)
            self.assertEqual(owned.stream.read(), self.original_bytes)
            self.assertNotEqual(self.original_bytes, replacement.read_bytes())
            self.assertFalse(moved_original.exists())
            self.assertEqual(
                replacement.read_bytes(), b"replacement owned by someone else"
            )
            self.assertEqual(self.source.read_bytes(), self.original_bytes)
            raise sqlite3.IntegrityError("registration failed")

        with (
            patch.object(OwnedWindowsFile, "create_new", side_effect=capture_owned),
            patch(
                "creator_loop.text_intake._new_id",
                side_effect=("asset-fixed", "file-fixed"),
            ),
            patch.object(
                LibraryRepository,
                "create_asset_file",
                autospec=True,
                side_effect=attempt_and_fail,
            ),
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "registration failed"):
                intake_text_original(self.source, root=self.root)

        print(
            "Windows ownership denials: "
            f"rename={attempts['rename']['winerror']}, "
            f"replace={attempts['replace']['winerror']}"
        )
        self.assertFalse(destination.exists())
        self.assertFalse(moved_original.exists())
        self.assertEqual(replacement.read_bytes(), b"replacement owned by someone else")
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(self._counts(), (0, 0))

    @unittest.skipUnless(os.name == "nt", "requires Windows disposition API")
    def test_disposition_failure_preserves_orphan_and_reports_original_error(
        self,
    ) -> None:
        with (
            patch(
                "creator_loop.text_intake._new_id",
                side_effect=("asset-fixed", "file-fixed"),
            ),
            patch.object(
                LibraryRepository,
                "create_asset_file",
                autospec=True,
                side_effect=sqlite3.IntegrityError("registration failed"),
            ),
            patch(
                "creator_loop.windows_owned_file._mark_for_deletion",
                side_effect=OSError("disposition denied"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "disposition denied") as caught:
                intake_text_original(self.source, root=self.root)

        self.assertIsInstance(caught.exception.__cause__, sqlite3.IntegrityError)
        self.assertEqual(self._destination().read_bytes(), self.original_bytes)
        with self._destination().open("r+b") as reopened:
            self.assertEqual(reopened.read(), self.original_bytes)
        self.assertEqual(self._counts(), (0, 0))


if __name__ == "__main__":
    unittest.main()
