"""Private fresh copier authorization/retention; full health proof comes separately."""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_corrupt_restore_copy as copy_fixture
from creator_loop.app_lock import AppDataLock
from creator_loop.corrupt_copy_recovery import recover_corrupt_copy
from creator_loop.corrupt_copy_resume import _continue_corrupt_copy, resume_corrupt_copy
from creator_loop.corrupt_restore_inspection import inspect_corrupt_copy
from creator_loop.corrupt_source_retention import hold_existing_corrupt_source
from creator_loop.fresh_restore_apply import copy_fresh_restore
from creator_loop.fresh_restore_review import _FreshRestoreRequest, review_fresh_restore
from creator_loop.restore_guard import require_no_pending_restore
from creator_loop.update_health import run_health_check


@unittest.skipUnless(os.name == "nt", "Native private fresh copier")
class FreshRestoreApplyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.fixture.fixture.installation
        self.copy = self.fixture.copy()
        self.copy_bytes = self.copy.read_bytes()

    def review(self):
        return review_fresh_restore(
            self.root,
            self.copy,
            self.fixture.fixture.backup_id,
            self.installation,
            self.fixture.fixture.candidate,
        )

    def apply(self, choice, **changes):
        return _continue_corrupt_copy(
            self.root,
            self.copy,
            self.installation,
            reviewed_inspection=None,
            confirm_lost_changes=changes.get("loss", True),
            confirm_preserve_unknown=changes.get("legacy_unknown", False),
            fresh_request=_FreshRestoreRequest(
                changes.get("backup_id", self.fixture.fixture.backup_id),
                self.fixture.fixture.candidate,
                changes.get("identity", choice["assessment_identity"]),
            ),
        )

    def files(self):
        return {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

    def test_actual_private_fresh_copy_keeps_whole_current_bundle_and_original_journal(
        self,
    ):
        current = {
            "creator_loop.sqlite3": b"PRIVATE CURRENT UNKNOWN DB",
            "creator_loop.sqlite3-wal": b"PRIVATE CURRENT WAL",
            "creator_loop.sqlite3-shm": b"PRIVATE CURRENT SHM",
            "creator_loop.sqlite3-journal": b"PRIVATE CURRENT HOT JOURNAL",
        }
        for name, payload in current.items():
            (self.root / name).write_bytes(payload)
        choice = self.review()
        before = self.files()
        record_path = self.apply(choice)
        receipt = json.loads(record_path.read_text())
        self.assertEqual(receipt["phase"], "FRESH_DB_COMMITTED_GUARDED")
        self.assertTrue(receipt["confirmed_fresh_restore"])
        self.assertFalse(receipt["confirmed_preserve_unknown"])
        self.assertFalse(receipt["consistent_current_backup"])
        retained = self.root / "backups" / receipt["retained_partial_directory"]
        self.assertEqual(
            {path.name: path.read_bytes() for path in retained.iterdir()}, current
        )
        for entry in receipt["retained_partial_files"]:
            stat = (retained / entry["name"]).stat()
            self.assertEqual(
                (stat.st_dev, stat.st_ino), (entry["device"], entry["inode"])
            )
        for path, payload in before.items():
            if path.parent != self.root and path.name != "restore-in-progress.json":
                self.assertEqual(path.read_bytes(), payload)
        guard = json.loads((self.root / "runtime/restore-in-progress.json").read_text())
        self.assertEqual(guard["fresh_restore_journals"], [record_path.name])
        archived_guard = (
            self.root / "backups" / f"corrupt-guard-{receipt['copy_id']}.json"
        )
        self.assertEqual(
            json.loads(archived_guard.read_text()),
            json.loads(before[self.root / "runtime/restore-in-progress.json"]),
        )
        self.assertEqual(self.copy.read_bytes(), self.copy_bytes)
        self.assertTrue((self.root / "runtime/restore-in-progress.json").exists())
        self.assertFalse((self.installation / "active-installation.json").exists())
        proof = inspect_corrupt_copy(self.root, self.copy)
        self.assertEqual(proof["actual_state"], "VALIDATED_COPY_GUARDED")
        self.assertEqual(len(proof["fresh_retention_history"]), 1)
        self.assertEqual(
            proof["fresh_retention_history"][0]["journal_name"], record_path.name
        )

    def test_retained_bytes_or_bound_metadata_tamper_never_allows_health_proof(self):
        self.fixture.source.write_bytes(b"PRIVATE CURRENT UNKNOWN")
        path = self.apply(self.review())
        record = json.loads(path.read_text())
        retained = (
            self.root
            / "backups"
            / record["retained_partial_directory"]
            / self.fixture.source.name
        )
        payload = retained.read_bytes()
        retained.write_bytes(b"TAMPERED CURRENT ORIGINAL")
        with self.assertRaisesRegex(ValueError, "Fresh original"):
            inspect_corrupt_copy(self.root, self.copy)
        retained.write_bytes(payload)
        record["selected_backup_id"] = "f" * 32
        path.write_text(json.dumps(record), encoding="utf-8")
        before = self.files()
        with self.assertRaisesRegex(ValueError, "mismatch"):
            inspect_corrupt_copy(self.root, self.copy)
        self.assertEqual(self.files(), before)
        self.assertTrue((self.root / "runtime/restore-in-progress.json").exists())

    def test_malformed_retained_names_types_and_fields_refuse_before_health(self):
        self.fixture.source.write_bytes(b"x")
        path = self.apply(self.review())
        original = path.read_bytes()
        token = inspect_corrupt_copy(self.root, self.copy)["inspection_identity"]
        for key, value in (
            ("name", []),
            ("byte_size", True),
            ("inode", 1.0),
            ("present", 1),
            ("EXTRA", "foreign"),
        ):
            with self.subTest(key=key):
                record = json.loads(original)
                record["retained_partial_files"][0][key] = value
                path.write_text(json.dumps(record), encoding="utf-8")
                before = self.files()
                with patch(
                    "creator_loop.corrupt_copy_recovery.run_health_check"
                ) as health:
                    with self.assertRaisesRegex(ValueError, "Fresh retained inventory"):
                        recover_corrupt_copy(
                            self.root,
                            self.copy,
                            self.installation,
                            reviewed_inspection=token,
                            confirm_recovery=True,
                        )
                    health.assert_not_called()
                self.assertEqual(self.files(), before)
                self.assertTrue(
                    (self.root / "runtime/restore-in-progress.json").exists()
                )
        path.write_bytes(original)  # Restore only our own malformed test metadata.
        self.assertEqual(
            inspect_corrupt_copy(self.root, self.copy)["inspection_identity"], token
        )

    def test_missing_anchored_journal_or_original_guard_archive_refuses_health(self):
        self.fixture.source.write_bytes(b"PRIVATE CURRENT UNKNOWN")
        journal = self.apply(self.review())
        proof = inspect_corrupt_copy(self.root, self.copy)
        copy_id = proof["copy_id"]
        for path in (journal, self.root / "backups" / f"corrupt-guard-{copy_id}.json"):
            hidden = path.with_name("OWN HIDDEN " + path.name)
            path.rename(hidden)  # Keep bytes; only our own exclusive fixture.
            before = self.files()
            with patch("creator_loop.corrupt_copy_recovery.run_health_check") as health:
                with self.assertRaises(ValueError):
                    recover_corrupt_copy(
                        self.root,
                        self.copy,
                        self.installation,
                        reviewed_inspection=proof["inspection_identity"],
                        confirm_recovery=True,
                    )
                health.assert_not_called()
            self.assertEqual(self.files(), before)
            self.assertTrue((self.root / "runtime/restore-in-progress.json").exists())
            hidden.rename(path)
        self.assertEqual(inspect_corrupt_copy(self.root, self.copy), proof)

    def test_actual_health_holds_fresh_originals_and_then_clears_guard(self):
        self.fixture.source.write_bytes(b"PRIVATE CURRENT UNKNOWN")
        path = self.apply(self.review())
        record = json.loads(path.read_text())
        retained = (
            self.root
            / "backups"
            / record["retained_partial_directory"]
            / self.fixture.source.name
        )
        before = retained.read_bytes()
        proof = inspect_corrupt_copy(self.root, self.copy)

        def health(executable, root, logs, **options):
            self.assertEqual(
                executable,
                self.fixture.fixture.candidate / "CreatorLoop/CreatorLoop.exe",
            )
            with self.assertRaises(OSError):
                retained.write_bytes(b"FOREIGN WRITE MUST FAIL")
            with self.assertRaises(OSError):
                path.write_bytes(b"FOREIGN METADATA MUST FAIL")
            with closing(sqlite3.connect(self.fixture.source, timeout=0.02)) as writer:
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute("BEGIN IMMEDIATE")
            return run_health_check(
                Path(sys.executable),
                root,
                logs,
                arguments_prefix=["-m", "creator_loop"],
                **options,
            )

        with patch("creator_loop.corrupt_copy_recovery.run_health_check", health):
            recover_corrupt_copy(
                self.root,
                self.copy,
                self.installation,
                reviewed_inspection=proof["inspection_identity"],
                confirm_recovery=True,
            )
        require_no_pending_restore(self.root)
        self.assertEqual(retained.read_bytes(), before)
        self.assertEqual(self.copy.read_bytes(), self.copy_bytes)

    def test_removed_guard_extension_cannot_hide_committed_fresh_history(self):
        self.fixture.source.write_bytes(b"PRIVATE CURRENT UNKNOWN")
        journal = self.apply(self.review())
        marker = self.root / "runtime/restore-in-progress.json"
        guard = json.loads(marker.read_text())
        del guard["fresh_restore_journals"]
        marker.write_text(json.dumps(guard), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unanchored fresh"):
            inspect_corrupt_copy(self.root, self.copy)
        journal.rename(journal.with_name("OWN HIDDEN " + journal.name))
        before = self.files()
        with self.assertRaisesRegex(ValueError, "no matching history"):
            inspect_corrupt_copy(self.root, self.copy)
        self.assertEqual(self.files(), before)

    def test_original_guard_archive_preserves_exact_bytes_not_reserialized_json(self):
        marker = self.root / "runtime/restore-in-progress.json"
        body = json.dumps(json.loads(marker.read_text()), indent=4) + "\n"
        marker.write_text(body, encoding="utf-8")  # Own valid fixture metadata.
        original = marker.read_bytes()
        self.fixture.source.write_bytes(b"PRIVATE CURRENT UNKNOWN")
        journal = self.apply(self.review())
        copy_id = json.loads(journal.read_text())["copy_id"]
        archived = self.root / "backups" / f"corrupt-guard-{copy_id}.json"
        self.assertEqual(archived.read_bytes(), original)

    def test_loss_legacy_consent_stale_or_foreign_choice_refuses_before_write(self):
        self.fixture.source.write_bytes(b"PRIVATE CURRENT UNKNOWN")
        choice = self.review()
        before = self.files()
        for changes in (
            {"loss": False},
            {"legacy_unknown": True},
            {"identity": "0" * 64},
            {"backup_id": "f" * 32},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                self.apply(choice, **changes)
            self.assertEqual(self.files(), before)
        proof = inspect_corrupt_copy(self.root, self.copy)
        with self.assertRaisesRegex(RuntimeError, "fresh restore decision"):
            resume_corrupt_copy(
                self.root,
                self.copy,
                self.installation,
                reviewed_inspection=proof["inspection_identity"],
                confirm_lost_changes=True,
                confirm_preserve_unknown=True,
            )
        self.assertEqual(self.files(), before)

    def test_same_bytes_replaced_inode_in_native_transition_refuses(self):
        payload = b"PRIVATE CURRENT UNKNOWN"
        self.fixture.source.write_bytes(payload)
        choice = self.review()

        def replace(path):
            if path == self.fixture.source:
                replacement = self.root / "OWN REPLACEMENT FIXTURE"
                replacement.write_bytes(payload)
                os.replace(replacement, path)
            return hold_existing_corrupt_source(path)

        with patch(
            "creator_loop.corrupt_copy_resume.hold_existing_corrupt_source", replace
        ):
            with self.assertRaisesRegex(RuntimeError, "inode changed"):
                self.apply(choice)
        self.assertEqual(self.fixture.source.read_bytes(), payload)
        self.assertFalse(
            list((self.root / "manifests").glob("corrupt-fresh-restore-*.json"))
        )
        self.assertEqual(self.copy.read_bytes(), self.copy_bytes)

    def test_actual_fresh_crashes_keep_split_bundle_then_retry_by_native_identity(self):
        for index, phase in enumerate(
            (
                "RESUME_INPUTS_PINNED",
                "FRESH_GUARD_ARCHIVE_PENDING",
                "FRESH_HISTORY_ANCHORED",
                "FRESH_CURRENT_RETENTION_CONTINUED",
                "FRESH_COPY_STARTED",
                "FRESH_DB_COMMITTED_GUARDED",
            )
        ):
            with self.subTest(phase=phase):
                if index:
                    self.setUp()
                current = {
                    self.fixture.source.name + suffix: b"PRIVATE UNKNOWN CURRENT "
                    + suffix.encode()
                    for suffix in ("", "-wal", "-shm", "-journal")
                }
                for name, payload in current.items():
                    (self.root / name).write_bytes(payload)
                choice = self.review()
                code = """
import os, sys
from pathlib import Path
import creator_loop.corrupt_copy_resume as m
import creator_loop.fresh_guard as g
from creator_loop.fresh_restore_review import _FreshRestoreRequest
original = m._journal
def crash(path, record):
    original(path, record)
    if record['phase'] == sys.argv[7]: os._exit(46)
m._journal = crash
publish = g._publish_new_manifest
def crash_archive(temporary, destination, stream):
    if sys.argv[7] == 'FRESH_GUARD_ARCHIVE_PENDING': os._exit(46)
    publish(temporary, destination, stream)
g._publish_new_manifest = crash_archive
m._continue_corrupt_copy(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]),
    reviewed_inspection=None, confirm_lost_changes=True,
    fresh_request=_FreshRestoreRequest(sys.argv[4], Path(sys.argv[5]), sys.argv[6]))
"""
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.root),
                        str(self.copy),
                        str(self.installation),
                        self.fixture.fixture.backup_id,
                        str(self.fixture.fixture.candidate),
                        choice["assessment_identity"],
                        phase,
                    ],
                    env={**os.environ, "PYTHONPATH": "app"},
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 46, result.stderr)
                proof = inspect_corrupt_copy(self.root, self.copy)
                self.assertEqual(len(proof["fresh_retention_history"]), 1)
                if proof["actual_state"] != "VALIDATED_COPY_GUARDED":
                    self.apply(self.review())
                    proof = inspect_corrupt_copy(self.root, self.copy)
                    self.assertEqual(len(proof["fresh_retention_history"]), 2)
                self.assertEqual(proof["actual_state"], "VALIDATED_COPY_GUARDED")
                for name, payload in current.items():
                    retained = list(
                        (self.root / "backups").glob(f"corrupt-original-*/{name}")
                    )
                    self.assertEqual(
                        sum(path.read_bytes() == payload for path in retained), 1
                    )
                self.assertEqual(self.copy.read_bytes(), self.copy_bytes)
                self.assertTrue(
                    (self.root / "runtime/restore-in-progress.json").exists()
                )

    def test_public_api_and_actual_cli_require_distinct_consent_then_keep_guard(self):
        self.fixture.source.write_bytes(b"PRIVATE CURRENT UNKNOWN")
        choice = self.review()
        before = self.files()
        for options in (
            {},
            {"confirm_fresh_restore": True},
            {"confirm_lost_changes": True},
        ):
            with self.assertRaises(ValueError):
                copy_fresh_restore(
                    self.root,
                    self.copy,
                    self.fixture.fixture.backup_id,
                    self.installation,
                    self.fixture.fixture.candidate,
                    reviewed_fresh_restore=choice["assessment_identity"],
                    **options,
                )
            self.assertEqual(self.files(), before)
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--apply-completed-copy-restore",
            str(self.copy),
            "--fresh-restore-backup",
            self.fixture.fixture.backup_id,
            "--installation-root",
            str(self.installation),
            "--restore-candidate",
            str(self.fixture.fixture.candidate),
            "--reviewed-fresh-restore",
            choice["assessment_identity"],
            "--confirm-fresh-restore",
            "--confirm-lost-changes",
        ]
        env = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }

        def run(arguments=command, environment=env):
            return subprocess.run(
                arguments, env=environment, capture_output=True, timeout=30
            )

        for arguments in (
            command[:-1],
            command + ["--confirm-preserve-unknown"],
            command + ["--recover-corrupt-copy", str(self.copy)],
            command + ["--smoke"],
        ):
            result = run(arguments)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(self.files(), before)
        with AppDataLock(self.root):
            result = run()
            self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.files(), before)
        stale = list(command)
        stale[stale.index("--reviewed-fresh-restore") + 1] = "0" * 64
        result = run(stale)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn(b"PRIVATE", result.stderr + result.stdout)
        self.assertEqual(self.files(), before)
        missing = self.root / "OWN MISSING ROOT"
        for flags in (
            ["--reviewed-fresh-restore", "0" * 64, "--smoke"],
            ["--confirm-fresh-restore", "--smoke"],
            ["--apply-completed-copy-restore", str(self.copy)],
        ):
            result = run(
                [sys.executable, "-m", "creator_loop", *flags],
                {**env, "CREATOR_LOOP_DATA_ROOT": str(missing)},
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(missing.exists())
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["phase"], "FRESH_DB_COMMITTED_GUARDED")
        self.assertTrue(receipt["guard_retained"])
        self.assertFalse(receipt["activated"])
        self.assertFalse(receipt["restored"])
        self.assertEqual(
            inspect_corrupt_copy(self.root, self.copy)["actual_state"],
            "VALIDATED_COPY_GUARDED",
        )
        self.assertEqual(self.copy.read_bytes(), self.copy_bytes)

    def test_missing_empty_and_changed_media_need_fresh_choice_and_separate_media_consent(
        self,
    ):
        for index, mode in enumerate(("missing", "empty", "media")):
            with self.subTest(mode=mode):
                if index:
                    self.setUp()
                if mode == "missing":
                    self.fixture.source.unlink()
                else:
                    self.fixture.source.write_bytes(
                        b"" if mode == "empty" else b"PRIVATE UNKNOWN"
                    )
                if mode == "media":
                    media = self.fixture.fixture.fixture.media
                    media.write_bytes(media.read_bytes() + b"CHANGED MEDIA KEPT")
                choice = self.review()
                options = dict(
                    reviewed_fresh_restore=choice["assessment_identity"],
                    confirm_fresh_restore=True,
                    confirm_lost_changes=True,
                )
                if mode == "media":
                    before = self.files()
                    with self.assertRaises(ValueError):
                        copy_fresh_restore(
                            self.root,
                            self.copy,
                            self.fixture.fixture.backup_id,
                            self.installation,
                            self.fixture.fixture.candidate,
                            **options,
                        )
                    self.assertEqual(self.files(), before)
                    options["confirm_media_issues"] = True
                path = copy_fresh_restore(
                    self.root,
                    self.copy,
                    self.fixture.fixture.backup_id,
                    self.installation,
                    self.fixture.fixture.candidate,
                    **options,
                )
                record = json.loads(path.read_text())
                if mode == "missing":
                    self.assertIsNone(record["retained_partial_directory"])
                elif mode == "empty":
                    retained = (
                        self.root
                        / "backups"
                        / record["retained_partial_directory"]
                        / self.fixture.source.name
                    )
                    self.assertEqual(retained.read_bytes(), b"")
                else:
                    self.assertTrue(media.read_bytes().endswith(b"CHANGED MEDIA KEPT"))
                    self.assertTrue(record["confirmed_media_issues"])
                self.assertEqual(
                    inspect_corrupt_copy(self.root, self.copy)["actual_state"],
                    "VALIDATED_COPY_GUARDED",
                )
                self.assertTrue(
                    (self.root / "runtime/restore-in-progress.json").exists()
                )

    def test_cancel_mid_fresh_retention_keeps_history_for_fresh_retry(self):
        import creator_loop.corrupt_copy_resume as copier

        self.fixture.source.write_bytes(b"PRIVATE CURRENT MAIN")
        (self.root / "creator_loop.sqlite3-wal").write_bytes(b"PRIVATE CURRENT WAL")
        choice = self.review()
        cancelled = False
        original = copier._journal

        def journal(path, record):
            nonlocal cancelled
            original(path, record)
            if record["phase"] == "FRESH_CURRENT_RETENTION_CONTINUED":
                cancelled = True

        with patch("creator_loop.corrupt_copy_resume._journal", journal):
            with self.assertRaises(InterruptedError):
                copy_fresh_restore(
                    self.root,
                    self.copy,
                    self.fixture.fixture.backup_id,
                    self.installation,
                    self.fixture.fixture.candidate,
                    reviewed_fresh_restore=choice["assessment_identity"],
                    confirm_fresh_restore=True,
                    confirm_lost_changes=True,
                    cancelled=lambda: cancelled,
                )
        proof = inspect_corrupt_copy(self.root, self.copy)
        self.assertEqual(len(proof["fresh_retention_history"]), 1)
        self.apply(self.review())
        proof = inspect_corrupt_copy(self.root, self.copy)
        self.assertEqual(len(proof["fresh_retention_history"]), 2)
        self.assertEqual(proof["actual_state"], "VALIDATED_COPY_GUARDED")
        self.assertTrue((self.root / "runtime/restore-in-progress.json").exists())
