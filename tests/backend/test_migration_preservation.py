"""Native full preservation and hostile isolated-restore inputs; no user data."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from creator_loop.backend.runtime.database import _connect_write, initialize
from creator_loop.backend.runtime.migration_preservation import (
    create_preservation_archive,
    verify_preservation_archive,
)
from creator_loop.backend.runtime.paths import ensure_data_root
from creator_loop.backend.runtime.update_backup import create_update_backup


@unittest.skipUnless(sys.platform == "win32", "native immutable Windows leases")
class MigrationPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Dữ liệu thử riêng"
        ensure_data_root(self.root)
        self.root = self.root.resolve(strict=True)
        initialize(self.root / "creator_loop.sqlite3")
        self.key = "storage/originals/tư liệu có dấu.txt"
        self.original = "Tư liệu nguyên bản, không thay đổi.".encode()
        self.media = self.root / self.key
        self.media.write_bytes(self.original)
        self.digest = hashlib.sha256(self.original).hexdigest()
        with _connect_write(self.root / "creator_loop.sqlite3") as db:
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                ("a", "TEXT", "fixture", "2026-10-21T00:00:00Z", None),
            )
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "f",
                    "a",
                    "ORIGINAL",
                    self.key,
                    self.digest,
                    len(self.original),
                    "text/plain",
                    "2026-10-21T00:00:00Z",
                ),
            )
        self.workspace = self.root / "runtime" / ("creator-workers-" + "a" * 32)
        self.workspace.mkdir()
        code = """import sys
from pathlib import Path
from creator_loop.backend.runtime.runtime_ownership import create_workspace_marker
root=Path(sys.argv[1]);work=Path(sys.argv[2])
create_workspace_marker(root,work,'creator-workers/fixture',kind='CREATOR_LLM_WORKSPACE')
(work/'prompt.txt').write_bytes(b'private unqualified historical prompt')
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "app")
        outcome = subprocess.run(
            [sys.executable, "-c", code, str(self.root), str(self.workspace)],
            env=environment,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(outcome.returncode, 0, outcome.stderr)

    def archive(self):
        return create_preservation_archive(self.root, timeout_seconds=30)

    def test_opaque_preserved_without_weakening_normal_backup_or_mutating_originals(
        self,
    ):
        before = {p.name: p.read_bytes() for p in self.workspace.iterdir()}
        with self.assertRaisesRegex(RuntimeError, "matching terminal owner"):
            create_update_backup(self.root)
        path = self.archive()
        with path.open("rb") as stream:
            restored = self.root.parent / "isolated restore"
            proof = verify_preservation_archive(stream, restored)
        self.assertEqual(proof["unmatched_runtime_workspaces"], 1)
        self.assertEqual(proof["integrity_and_foreign_keys"], "PASS")
        self.assertFalse(proof["cloud_restore_verified"])
        self.assertFalse(proof["permits_migration_or_cleanup"])
        self.assertEqual((restored / self.key).read_bytes(), self.original)
        self.assertFalse((restored / "runtime").exists())
        for name, content in before.items():
            self.assertEqual(
                (
                    restored / "evidence/runtime" / self.workspace.name / name
                ).read_bytes(),
                content,
            )
            self.assertEqual((self.workspace / name).read_bytes(), content)
        self.assertEqual(self.media.read_bytes(), self.original)
        with self.assertRaisesRegex(RuntimeError, "matching terminal owner"):
            create_update_backup(self.root)

    def test_digest_corruption_refuses_publication_and_keeps_original(self):
        self.media.write_bytes(b"incorrect content")
        with self.assertRaisesRegex((ValueError, RuntimeError), "mismatch|differs"):
            self.archive()
        self.assertEqual(list((self.root / "backups").glob("preservation-*.zip")), [])
        self.assertEqual(self.media.read_bytes(), b"incorrect content")

    def test_live_owner_is_refused(self):
        from creator_loop.backend.runtime.runtime_ownership import (
            create_workspace_marker,
        )

        live = self.root / "runtime" / ("creator-workers-" + "b" * 32)
        live.mkdir()
        create_workspace_marker(
            self.root, live, "creator-workers/live", kind="CREATOR_LLM_WORKSPACE"
        )
        with self.assertRaisesRegex(RuntimeError, "verified dead"):
            self.archive()
        self.assertEqual(list((self.root / "backups").glob("preservation-*.zip")), [])

    def test_untrusted_archive_digest_and_path_rejected(self):
        path = self.archive()
        with zipfile.ZipFile(path) as source:
            contents = {name: source.read(name) for name in source.namelist()}
        for attack in ("digest", "traversal", "locator", "windows-alias", "device"):
            modified = contents.copy()
            manifest = json.loads(modified["preservation.json"])
            if attack == "digest":
                modified["media/" + self.digest] = b"corrupted"
            elif attack in ("traversal", "windows-alias", "device"):
                name = {
                    "traversal": "../outside",
                    "windows-alias": "evidence/runtime/.. /.. /.. /outside",
                    "device": "evidence/runtime/CON.txt",
                }[attack]
                modified[name] = b"escape"
                manifest["files"][name] = {
                    "bytes": 6,
                    "sha256": hashlib.sha256(b"escape").hexdigest(),
                }
            else:
                manifest["media"][0]["storage_key"] = "../outside"
            modified["preservation.json"] = json.dumps(manifest).encode()
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                for name, body in modified.items():
                    archive.writestr(name, body)
            stream.seek(0)
            with self.subTest(attack=attack), self.assertRaises((ValueError, KeyError)):
                verify_preservation_archive(
                    stream, self.root.parent / ("rejected-" + attack)
                )
        self.assertFalse((self.root.parent / "outside").exists())
