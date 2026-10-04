"""Resume actual interrupted copies with consent, retaining originals/partials."""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_corrupt_restore_copy as copy_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.corrupt_copy_resume import resume_corrupt_copy
from creator_loop.corrupt_restore_inspection import inspect_corrupt_copy
from creator_loop.restore_guard import PendingRestore, require_no_pending_restore


@unittest.skipUnless(sys.platform == "win32", "actual native interrupted copies")
class CorruptCopyResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.fixture.fixture.installation

    def crash_copy(self, phase):
        f = self.fixture
        code = """
import os,sys
from pathlib import Path
import creator_loop.corrupt_restore_copy as m
original=m._journal
def crash(path,record):
    original(path,record)
    if record['phase']==sys.argv[7]: os._exit(41)
m._journal=crash
m.copy_corrupt_restore(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),reviewed_preparation=sys.argv[5],reviewed_identity=sys.argv[6],confirm_lost_changes=True)
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(self.root),
                str(f.manifest),
                str(self.installation),
                str(f.fixture.candidate),
                f.preparation_sha,
                f.fixture.assessment["assessment_identity"],
                phase,
            ],
            env={**os.environ, "PYTHONPATH": "app"},
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 41, result.stderr)
        self.copy = next((self.root / "manifests").glob("corrupt-restore-*.json"))
        self.copy_bytes = self.copy.read_bytes()
        self.proof = inspect_corrupt_copy(self.root, self.copy)

    def resume(self, **changes):
        options = {
            "reviewed_inspection": self.proof["inspection_identity"],
            "confirm_lost_changes": True,
            **changes,
        }
        return resume_corrupt_copy(self.root, self.copy, self.installation, **options)

    def assert_guarded(self):
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        self.assertEqual(self.copy.read_bytes(), self.copy_bytes)
        self.fixture.assert_retained_or_original()

    def test_actual_unmoved_partial_missing_empty_copies_resume_without_overwrite(self):
        phases = [
            "CORRUPT_COPY_GUARDED",
            "CORRUPT_SOURCE_RETENTION_STARTED",
            "CORRUPT_SOURCE_RETAINED",
            "CORRUPT_COPY_STARTED",
        ]
        for index, phase in enumerate(phases):
            with self.subTest(phase=phase):
                if index:
                    self.setUp()
                self.crash_copy(phase)
                journal = self.resume(
                    confirm_keep_partial=phase == "CORRUPT_COPY_STARTED"
                )
                record = json.loads(journal.read_text())
                self.assertEqual(record["phase"], "RESUME_DB_COMMITTED_GUARDED")
                self.assertFalse(record["restored"])
                self.assertFalse(record["consistent_partial_backup"])
                if phase == "CORRUPT_COPY_STARTED":
                    partial = (
                        self.root
                        / "backups"
                        / record["retained_partial_directory"]
                        / "creator_loop.sqlite3"
                    )
                    self.assertEqual(partial.read_bytes(), b"")
                else:
                    self.assertIsNone(record["retained_partial_directory"])
                current = inspect_corrupt_copy(self.root, self.copy)
                self.assertEqual(current["actual_state"], "VALIDATED_COPY_GUARDED")
                self.assert_guarded()

    def test_empty_partial_needs_explicit_preservation_consent(self):
        self.crash_copy("CORRUPT_COPY_STARTED")
        before = self.fixture.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "preserve the empty partial"):
            self.resume()
        self.assertEqual(self.fixture.source.read_bytes(), before)
        self.assertFalse(list((self.root / "manifests").glob("corrupt-resume-*.json")))
        self.assert_guarded()

    def test_loss_consent_stale_review_busy_and_unknown_refuse_unchanged(self):
        self.crash_copy("CORRUPT_SOURCE_RETAINED")
        with self.assertRaises(ValueError):
            self.resume(confirm_lost_changes=False)
        with self.assertRaises(ValueError):
            self.resume(reviewed_inspection="0" * 64)
        with AppDataLock(self.root), self.assertRaises(DataRootBusy):
            self.resume()
        self.fixture.source.write_bytes(b"KEEP UNKNOWN NONEMPTY TARGET")
        self.proof = inspect_corrupt_copy(self.root, self.copy)
        before = self.fixture.source.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Unknown or already copied"):
            self.resume(confirm_keep_partial=True)
        self.assertEqual(self.fixture.source.read_bytes(), before)
        self.assert_guarded()

    def test_live_gap_change_is_retained_without_any_original_move(self):
        from creator_loop.corrupt_copy_resume import hold_corrupt_preparation

        self.crash_copy("CORRUPT_COPY_GUARDED")

        def gap(*args, **kwargs):
            self.fixture.source.write_bytes(b"KEEP GAP CHANGES")
            return hold_corrupt_preparation(*args, **kwargs)

        with patch("creator_loop.corrupt_copy_resume.hold_corrupt_preparation", gap):
            with self.assertRaisesRegex(RuntimeError, "Live bytes changed"):
                self.resume()
        self.assertEqual(self.fixture.source.read_bytes(), b"KEEP GAP CHANGES")
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        self.assertFalse(list((self.root / "manifests").glob("corrupt-resume-*.json")))

    def test_cancel_after_retention_keeps_guard_and_originals_for_next_inspection(self):
        from creator_loop.update_preparation import _journal

        self.crash_copy("CORRUPT_COPY_GUARDED")
        cancelled = False

        def journal(path, record):
            nonlocal cancelled
            _journal(path, record)
            if record.get("phase") == "ORIGINAL_RETENTION_CONTINUED":
                cancelled = True

        with patch("creator_loop.corrupt_copy_resume._journal", journal):
            with self.assertRaises(InterruptedError):
                self.resume(cancelled=lambda: cancelled)
        self.assert_guarded()
        self.proof = inspect_corrupt_copy(self.root, self.copy)
        self.resume()
        self.assertEqual(
            inspect_corrupt_copy(self.root, self.copy)["actual_state"],
            "VALIDATED_COPY_GUARDED",
        )
        self.assert_guarded()

    def test_actual_cli_refuses_orphans_mixed_stale_busy_then_copies_guarded(self):
        self.crash_copy("CORRUPT_COPY_STARTED")
        environment = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        prefix = [sys.executable, "-m", "creator_loop"]
        base = prefix + [
            "--resume-corrupt-copy",
            str(self.copy),
            "--reviewed-inspection",
            self.proof["inspection_identity"],
            "--installation-root",
            str(self.installation),
        ]

        def run(args):
            return subprocess.run(
                args, env=environment, capture_output=True, timeout=30
            )

        for orphan in (
            ["--confirm-keep-partial"],
            ["--confirm-recovery"],
            ["--reviewed-inspection", self.proof["inspection_identity"]],
        ):
            result = run(prefix + orphan)
            self.assertEqual(result.returncode, 2, result.stderr)
        for extra in (
            [],
            ["--confirm-lost-changes", "--smoke"],
            ["--confirm-lost-changes", "--confirm-recovery"],
            ["--confirm-lost-changes", "--recover-corrupt-copy", str(self.copy)],
        ):
            result = run(base + extra)
            self.assertEqual(result.returncode, 2, result.stderr)
        confirmed = base + ["--confirm-lost-changes", "--confirm-keep-partial"]
        stale = [
            "0" * 64 if value == self.proof["inspection_identity"] else value
            for value in confirmed
        ]
        result = run(stale)
        self.assertEqual(result.returncode, 4, result.stderr)
        with AppDataLock(self.root):
            result = run(confirmed)
            self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.fixture.source.read_bytes(), b"")
        self.assertFalse(list((self.root / "manifests").glob("corrupt-resume-*.json")))
        result = run(confirmed)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["activated"])
        self.assertFalse(receipt["restored"])
        self.assertTrue(receipt["guard_retained"])
        self.assertTrue((self.root / "manifests" / receipt["journal_name"]).is_file())
        self.assertEqual(
            inspect_corrupt_copy(self.root, self.copy)["actual_state"],
            "VALIDATED_COPY_GUARDED",
        )
        self.assert_guarded()

    def test_foreign_retention_file_during_copy_prevents_verified_receipt(self):
        import creator_loop.corrupt_copy_resume as resume_module

        self.crash_copy("CORRUPT_SOURCE_RETAINED")
        original = resume_module.validate
        retained = (
            self.root / "backups" / json.loads(self.copy_bytes)["retained_directory"]
        )
        foreign = retained / "KEEP-FOREIGN-BYTES"

        def changed(db):
            original(db)
            foreign.write_bytes(b"PRIVATE FOREIGN FILE")

        with patch("creator_loop.corrupt_copy_resume.validate", changed):
            with self.assertRaisesRegex(RuntimeError, "before verified receipt"):
                self.resume()
        self.assertEqual(foreign.read_bytes(), b"PRIVATE FOREIGN FILE")
        record = json.loads(
            next((self.root / "manifests").glob("corrupt-resume-*.json")).read_text()
        )
        self.assertEqual(record["phase"], "RESUME_COPY_STARTED")
        self.assert_guarded()

    def test_actual_resume_crashes_preserve_bytes_and_fresh_retry_classifies_state(
        self,
    ):
        phases = [
            "RESUME_INPUTS_PINNED",
            "EMPTY_PARTIAL_RETAINED",
            "RESUME_COPY_STARTED",
            "RESUME_DB_COMMITTED_GUARDED",
        ]
        for index, phase in enumerate(phases):
            with self.subTest(phase=phase):
                if index:
                    self.setUp()
                self.crash_copy("CORRUPT_COPY_STARTED")
                code = """
import os,sys
from pathlib import Path
import creator_loop.corrupt_copy_resume as m
original=m._journal
def crash(path,record):
    original(path,record)
    if record['phase']==sys.argv[4]: os._exit(42)
m._journal=crash
m.resume_corrupt_copy(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),reviewed_inspection=sys.argv[5],confirm_lost_changes=True,confirm_keep_partial=True)
"""
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.root),
                        str(self.copy),
                        str(self.installation),
                        phase,
                        self.proof["inspection_identity"],
                    ],
                    env={**os.environ, "PYTHONPATH": "app"},
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assert_guarded()
                record = json.loads(
                    next(
                        (self.root / "manifests").glob("corrupt-resume-*.json")
                    ).read_text()
                )
                partial = (
                    self.root
                    / "backups"
                    / record["retained_partial_directory"]
                    / "creator_loop.sqlite3"
                )
                if phase != "RESUME_INPUTS_PINNED":
                    self.assertEqual(partial.read_bytes(), b"")
                self.proof = inspect_corrupt_copy(self.root, self.copy)
                if phase == "RESUME_DB_COMMITTED_GUARDED":
                    with self.assertRaises(RuntimeError):
                        self.resume(confirm_keep_partial=True)
                else:
                    self.resume(confirm_keep_partial=True)
                self.assertEqual(
                    inspect_corrupt_copy(self.root, self.copy)["actual_state"],
                    "VALIDATED_COPY_GUARDED",
                )
                self.assert_guarded()
