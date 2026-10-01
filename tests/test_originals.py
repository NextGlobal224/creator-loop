import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from creator_loop.database import initialize
from creator_loop.originals import (
    READ_CHUNK_SIZE,
    OriginalFileVerificationError,
    verify_original_file,
)
from creator_loop.paths import ensure_data_root


class OriginalFileVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "user data"
        ensure_data_root(self.root)
        db_path = self.root / "creator_loop.sqlite3"
        initialize(db_path)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        self.db.execute(
            """
            INSERT INTO assets (asset_id, media_type, display_name, created_at)
            VALUES ('asset-1', 'TEXT', 'Synthetic file', '2026-10-01T00:00:00Z')
            """
        )
        self.db.commit()

    def _record(
        self,
        *,
        file_id: str = "file-1",
        key: str = "storage/originals/source.txt",
        content: bytes = b"original bytes",
        role: str = "ORIGINAL",
        byte_size: int | None = None,
        digest: str | None = None,
        create_file: bool = True,
    ) -> Path:
        path = self.root.joinpath(*key.split("/"))
        if create_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.db.execute(
            """
            INSERT INTO asset_files (
                file_id, asset_id, role, storage_key, sha256,
                byte_size, mime_type, created_at
            )
            VALUES (?, 'asset-1', ?, ?, ?, ?, 'text/plain', ?)
            """,
            (
                file_id,
                role,
                key,
                hashlib.sha256(content).hexdigest() if digest is None else digest,
                len(content) if byte_size is None else byte_size,
                "2026-10-01T00:00:00Z",
            ),
        )
        self.db.commit()
        return path

    def _metadata(self, file_id: str = "file-1") -> tuple:
        return self.db.execute(
            """
            SELECT file_id, role, storage_key, byte_size, sha256
            FROM asset_files WHERE file_id = ?
            """,
            (file_id,),
        ).fetchone()

    def _assert_failure(self, reason: str, file_id: str = "file-1") -> None:
        before = self._metadata(file_id)
        with self.assertRaises(OriginalFileVerificationError) as caught:
            verify_original_file(self.db, file_id, self.root)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self._metadata(file_id), before)

    def test_valid_original_matches_recorded_metadata(self) -> None:
        path = self._record()
        before = self._metadata()

        self.assertIsNone(verify_original_file(self.db, "file-1", self.root))

        self.assertEqual(self._metadata(), before)
        self.assertEqual(path.read_bytes(), b"original bytes")

    def test_spaces_and_vietnamese_filename(self) -> None:
        path = self._record(key="storage/originals/thư mục có dấu/Đà Nẵng.txt")

        self.assertIsNone(verify_original_file(self.db, "file-1", self.root))
        self.assertEqual(path.read_bytes(), b"original bytes")

    def test_file_larger_than_one_chunk(self) -> None:
        content = b"a" * (READ_CHUNK_SIZE + 17)
        self._record(content=content)

        self.assertIsNone(verify_original_file(self.db, "file-1", self.root))

    def test_changed_bytes_fail_digest_check_without_repair(self) -> None:
        path = self._record(content=b"first")
        path.write_bytes(b"other")

        self._assert_failure("digest_mismatch")
        self.assertEqual(path.read_bytes(), b"other")

    def test_wrong_recorded_size_fails_without_repair(self) -> None:
        path = self._record(content=b"abc", byte_size=4)

        self._assert_failure("size_mismatch")
        self.assertEqual(path.read_bytes(), b"abc")

    def test_malformed_recorded_digest_fails_without_repair(self) -> None:
        path = self._record(digest="sha256:test")

        self._assert_failure("malformed_digest")
        self.assertEqual(path.read_bytes(), b"original bytes")

    def test_missing_physical_file_does_not_delete_metadata(self) -> None:
        self._record(create_file=False)

        self._assert_failure("missing_file")

    def test_non_original_row_is_rejected(self) -> None:
        path = self._record(role="DERIVED_AUDIO")

        self._assert_failure("invalid_role")
        self.assertEqual(path.read_bytes(), b"original bytes")

    def test_absolute_storage_keys_are_rejected(self) -> None:
        for index, key in enumerate(("C:\\outside\\file.txt", "/outside/file.txt")):
            file_id = f"absolute-{index}"
            self._record(file_id=file_id, key=key, create_file=False)
            with self.subTest(key=key):
                self._assert_failure("unsafe_path", file_id)

    def test_parent_traversal_is_rejected(self) -> None:
        outside = self.root / "storage" / "derived" / "outside.txt"
        outside.write_bytes(b"original bytes")
        self._record(key="storage/originals/../derived/outside.txt", create_file=False)

        self._assert_failure("unsafe_path")
        self.assertEqual(outside.read_bytes(), b"original bytes")

    def test_symlink_escape_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_bytes(b"original bytes")
        link = self.root / "storage" / "originals" / "escape.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        self._record(key="storage/originals/escape.txt", create_file=False)

        self._assert_failure("unsafe_path")
        self.assertEqual(outside.read_bytes(), b"original bytes")

    def test_missing_file_id_is_reported(self) -> None:
        self._assert_failure("missing_row", "missing-file-id")
