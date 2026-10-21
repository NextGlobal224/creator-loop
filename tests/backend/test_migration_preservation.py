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
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

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
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db, db:
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

    def test_creator_parent_and_child_extra_proof_is_preservation_only(self):
        from creator_loop.backend.runtime import creator_backup_runtime as guard

        # Produce real bound parent/child records in this fixture, both exited.
        self.workspace = self.root / "runtime" / ("creator-workers-" + "b" * 32)
        self.workspace.mkdir()
        code = """import sys,json
from pathlib import Path
from creator_loop.backend.runtime.runtime_ownership import create_workspace_marker
from creator_loop.backend.runtime.owned_process import OwnedWindowsProcess
root=Path(sys.argv[1]);work=Path(sys.argv[2])
create_workspace_marker(root,work,'creator-workers/fixture',kind='CREATOR_LLM_WORKSPACE')
def record(value): (work/'child-ownership.json').write_text(json.dumps(value),encoding='utf-8')
with OwnedWindowsProcess(Path(sys.executable),['-c','pass'],work/'process',component_version='creator-workers/fixture',before_resume=record) as child:
 child.wait(5)
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.root), str(self.workspace)],
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "app"),
            },
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        error = PermissionError("native handle unavailable")
        error.winerror = 5
        with (
            patch.object(guard, "_identity_dead", side_effect=error),
            patch.object(
                guard,
                "preservation_identity_dead",
                wraps=guard.preservation_identity_dead,
            ) as extra,
        ):
            with self.assertRaises(PermissionError):
                create_update_backup(self.root)
            extra.assert_not_called()
            path = self.archive()
            extra.assert_any_call(
                json.loads((self.workspace / "ownership.json").read_text("utf-8"))[
                    "parent"
                ]
            )
            extra.assert_any_call(
                json.loads((self.workspace / "child-ownership.json").read_text("utf-8"))
            )
            with path.open("rb") as stream:
                proof = verify_preservation_archive(
                    stream, self.root.parent / "creator isolated"
                )
            self.assertTrue(proof["all_archive_bytes_verified"])
        with patch.object(guard, "preservation_identity_dead", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "verified dead"):
                self.archive()

    def historical_workspace(self, family, *, root=None):
        root = root or self.root
        workspace = root / "runtime" / (family + "-" + "c" * 32)
        workspace.mkdir()
        if family == "voice-request":
            (workspace / "voice-request.txt").write_text(
                "Giữ nguyên lời đã nhập.", "utf-8"
            )
            return workspace
        code = """import sys
from pathlib import Path
from creator_loop.backend.runtime.runtime_ownership import create_workspace_marker
root=Path(sys.argv[1]);work=Path(sys.argv[2]);family=sys.argv[3]
kind='FACEBOOK_BROWSER_WORKSPACE' if family=='facebook' else 'AC_COMPONENT_WORKSPACE'
create_workspace_marker(root,work,family+'/historical-fixture',kind=kind)
(work/'request.json').write_text('{"fixture":"historical, never execute"}',encoding='utf-8')
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(root), str(workspace), family],
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "app"),
            },
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return workspace

    def test_historical_A_component_and_unowned_text_bytes_are_quarantined(self):
        workspaces = [
            self.historical_workspace(family)
            for family in (
                "facebook",
                "semantic",
                "piper",
                "vieneu-nano",
                "voice-request",
            )
        ]
        originals = {p: p.read_bytes() for w in workspaces for p in w.iterdir()}
        path = self.archive()
        isolated = self.root.parent / "reopened history"
        with path.open("rb") as stream:
            proof = verify_preservation_archive(stream, isolated)
        self.assertFalse(proof["permits_migration_or_cleanup"])
        self.assertFalse(proof["runtime_evidence_executed"])
        self.assertFalse((isolated / "runtime").exists())
        for p, original in originals.items():
            self.assertEqual(p.read_bytes(), original)
            self.assertEqual(
                (
                    isolated / "evidence/runtime" / p.relative_to(self.root / "runtime")
                ).read_bytes(),
                original,
            )
        with zipfile.ZipFile(path) as archive:
            opaque = json.loads(archive.read("preservation.json"))["opaque_runtime"]
        self.assertTrue(
            any(
                x["provenance"] == "UNOWNED_INPUT_DESCRIPTOR_NOT_EXECUTOR"
                for x in opaque
            )
        )

    def test_ordinary_backup_still_refuses_A_history_without_B_workspace(self):
        root = self.root.parent / "ordinary only A"
        ensure_data_root(root)
        root = root.resolve(strict=True)
        initialize(root / "creator_loop.sqlite3")
        self.historical_workspace("facebook", root=root)
        with self.assertRaisesRegex(RuntimeError, "recovery before backup"):
            create_update_backup(root)
        create_preservation_archive(root, timeout_seconds=30)

    def test_historical_owner_binding_live_owner_and_unknown_entries_refuse(self):
        from creator_loop.backend.runtime.runtime_ownership import (
            create_workspace_marker,
        )

        workspace = self.historical_workspace("semantic")
        marker_path = workspace / "ownership.json"
        original = marker_path.read_bytes()
        marker = json.loads(original)
        marker["data_root"] = str(self.root.parent)
        marker_path.write_text(json.dumps(marker), "utf-8")
        with self.assertRaisesRegex(RuntimeError, "verified dead"):
            self.archive()
        marker_path.write_bytes(original)
        (workspace / "unexpected.exe").write_bytes(
            b"not executable, unknown input fixture"
        )
        with self.assertRaisesRegex(RuntimeError, "Unknown historical"):
            self.archive()
        (workspace / "unexpected.exe").unlink()
        marker_path.unlink()
        create_workspace_marker(
            self.root, workspace, "semantic/live", kind="AC_COMPONENT_WORKSPACE"
        )
        with self.assertRaisesRegex(RuntimeError, "verified dead"):
            self.archive()

    def test_voice_descriptor_cannot_hide_an_executor_or_unknown_payload(self):
        workspace = self.historical_workspace("voice-request")
        (workspace / "ownership.json").write_text("{}", "utf-8")
        with self.assertRaisesRegex(RuntimeError, "Unknown voice input"):
            self.archive()
        self.assertEqual(list((self.root / "backups").glob("preservation-*.zip")), [])

    def historical_media(self):
        """Existing-output fixture only; no renderer/process/model is launched."""
        key = "storage/derived/old-result.mp4"
        payload = b"preserve old opaque output bytes; not a playable quality sample"
        (self.root / key).write_bytes(payload)
        timestamp = "2026-10-21T00:00:00Z"
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db, db:
            db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?)",
                ("p", "fixture", "ACTIVE", timestamp, None),
            )
            db.execute(
                "INSERT INTO drafts VALUES (?,?,?,?)", ("d", "p", "ACTIVE", timestamp)
            )
            db.execute(
                "INSERT INTO draft_versions(draft_version_id,draft_id,version_no,body_text,format,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
                ("v", "d", 1, "historical fixture", "VIDEO", "fixture", timestamp),
            )
            db.execute("INSERT INTO draft_version_seals VALUES (?,?)", ("v", timestamp))
            db.execute(
                "INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,finished_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "r",
                    "a",
                    "f",
                    "DRAFT_VIDEO_RENDER",
                    "SUCCEEDED",
                    "historical-fixture",
                    "1",
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                "INSERT INTO draft_render_requests VALUES (?,?,?,?,?)",
                ("r", "v", "{}", "old fixture", timestamp),
            )
            db.execute(
                "INSERT INTO draft_render_outputs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "r",
                    key,
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                    "video/mp4",
                    1,
                    1,
                    1,
                    "historical-fixture",
                    "1",
                    timestamp,
                ),
            )
        return key, payload

    def test_historical_output_outside_asset_files_is_preserved_and_reopened(self):
        key, payload = self.historical_media()
        path = self.archive()
        with path.open("rb") as stream:
            isolated = self.root.parent / "historical reopen"
            proof = verify_preservation_archive(stream, isolated)
        self.assertEqual(proof["historical_render_media_records"], 1)
        self.assertEqual((isolated / key).read_bytes(), payload)
        self.assertEqual((self.root / key).read_bytes(), payload)
        self.assertFalse(proof["runtime_evidence_executed"])

    def test_missing_historical_output_refuses_full_preservation(self):
        key, _ = self.historical_media()
        (self.root / key).unlink()
        with self.assertRaises(OSError):
            self.archive()
        self.assertEqual(list((self.root / "backups").glob("preservation-*.zip")), [])

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
