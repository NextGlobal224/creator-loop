"""Actual guarded-copy recovery, native health, state refusal and crash recovery."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import test_corrupt_restore_copy as copy_fixture
from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.corrupt_copy_recovery import recover_corrupt_copy
from creator_loop.corrupt_restore_inspection import inspect_corrupt_copy
from creator_loop.restore_guard import PendingRestore, require_no_pending_restore
from creator_loop.update_activation import active_candidate
from creator_loop.update_backup import _digest
from creator_loop.update_health import readonly_health, run_health_check


@unittest.skipUnless(sys.platform == "win32", "actual guarded Windows recovery")
class CorruptCopyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.copy = self.fixture.copy()
        self.installation = self.fixture.fixture.fixture.installation
        self.candidate = self.fixture.fixture.candidate
        self.inspection = inspect_corrupt_copy(self.root, self.copy)
        self.token = self.inspection["inspection_identity"]
        self.source = self.root / "creator_loop.sqlite3"
        self.before = self.source.read_bytes()
        self.copy_bytes = self.copy.read_bytes()
        self.pointer = self.installation / "active-installation.json"
        self.recovery = (
            self.root
            / "manifests"
            / f"corrupt-recovery-{self.inspection['copy_id']}.json"
        )

    def recover(self, **changes):
        options = {
            "reviewed_inspection": self.token,
            "confirm_recovery": True,
            **changes,
        }
        return recover_corrupt_copy(self.root, self.copy, self.installation, **options)

    def assert_kept(self):
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertEqual(self.copy.read_bytes(), self.copy_bytes)
        self.fixture.assert_retained_or_original()

    def guarded(self):
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        self.assert_kept()

    def actual_health(self, executable, root, logs, **options):
        self.assertEqual(executable, self.candidate / "CreatorLoop/CreatorLoop.exe")
        self.assertEqual(
            json.loads(self.pointer.read_text())["status"], "PENDING_HEALTH"
        )
        with self.assertRaises(DataRootBusy), AppDataLock(root):
            self.fail("Recovery dropped its app lock")
        with closing(sqlite3.connect(self.source, timeout=0.02)) as writer:
            with self.assertRaises(sqlite3.OperationalError):
                writer.execute("BEGIN IMMEDIATE")
        # Real source entrypoint/owned native health child, not the fixture's
        # stub installation. Exact candidate EXE is a separate artifact probe.
        return run_health_check(
            Path(sys.executable),
            root,
            logs,
            arguments_prefix=["-m", "creator_loop"],
            **options,
        )

    def test_actual_owned_health_and_continuous_locks_then_guard_clear(self):
        with patch(
            "creator_loop.corrupt_copy_recovery.run_health_check", self.actual_health
        ):
            self.assertEqual(self.recover(), self.candidate)
        require_no_pending_restore(self.root)
        self.assertEqual(active_candidate(self.installation, 6)[0], self.candidate)
        record = json.loads(self.recovery.read_text())
        self.assertEqual(record["phase"], "RESTORE_COMPLETED")
        self.assertEqual(record["restore_format"], 2)
        self.assertTrue(record["restored"])
        self.assertFalse(record["activation_pending"])
        self.assertNotIn("pre_restore_backup_id", record)
        self.assert_kept()

    def test_missing_consent_or_stale_review_refuses_before_rw_or_health(self):
        with patch("creator_loop.corrupt_copy_recovery.run_health_check") as health:
            for options in (
                {"confirm_recovery": False},
                {"reviewed_inspection": "0" * 64},
            ):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.recover(**options)
            health.assert_not_called()
        self.assertFalse(self.pointer.exists())
        self.guarded()

    def test_unknown_current_database_never_opens_rw_or_recopies(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE assets SET display_name='KEEP FOREIGN CHANGES'")
            db.commit()
        self.before = self.source.read_bytes()
        self.token = inspect_corrupt_copy(self.root, self.copy)["inspection_identity"]
        with (
            patch(
                "creator_loop.corrupt_copy_recovery.sqlite3.connect",
                wraps=sqlite3.connect,
            ) as connect,
            patch("creator_loop.corrupt_copy_recovery.run_health_check") as health,
        ):
            with self.assertRaisesRegex(RuntimeError, "Unconfirmed copy"):
                self.recover()
            health.assert_not_called()
            self.assertTrue(
                all(
                    "mode=rw" not in str(call.args[0])
                    for call in connect.call_args_list
                )
            )
        self.guarded()

    def test_backup_candidate_and_current_media_tamper_refuse_before_health(self):
        record = json.loads(self.copy.read_text())
        paths = [
            self.root / "backups" / record["backup_id"] / "creator_loop.sqlite3",
            self.candidate / "CreatorLoop/CreatorLoop.exe",
            self.fixture.fixture.fixture.media,
        ]
        for path in paths:
            with self.subTest(path=path):
                body = path.read_bytes()
                path.write_bytes(body + b"KEEP TAMPER")
                with patch(
                    "creator_loop.corrupt_copy_recovery.run_health_check"
                ) as health:
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.recover()
                    health.assert_not_called()
                self.assertFalse(self.pointer.exists())
                path.write_bytes(body)  # only our own synthetic test fixture
        self.guarded()

    def test_failed_health_keeps_guard_originals_and_can_retry_fresh(self):
        with patch(
            "creator_loop.corrupt_copy_recovery.run_health_check",
            side_effect=TimeoutError("PRIVATE ERROR"),
        ):
            with self.assertRaises(TimeoutError):
                self.recover()
        self.assertEqual(
            json.loads(self.pointer.read_text())["status"], "HEALTH_FAILED"
        )
        self.assertNotIn("PRIVATE ERROR", self.recovery.read_text())
        self.guarded()
        with patch(
            "creator_loop.corrupt_copy_recovery.run_health_check", self.actual_health
        ):
            self.recover()
        require_no_pending_restore(self.root)
        self.assert_kept()

    def test_metadata_failure_retains_active_pointer_and_guard_until_fresh_retry(self):
        with (
            patch(
                "creator_loop.corrupt_copy_recovery.run_health_check",
                self.actual_health,
            ),
            patch(
                "creator_loop.corrupt_copy_recovery.record_successful_update",
                side_effect=OSError("PRIVATE METADATA"),
            ),
        ):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(json.loads(self.pointer.read_text())["status"], "ACTIVE")
        self.assertEqual(
            json.loads(self.recovery.read_text())["phase"], "COMPLETED_METADATA_PENDING"
        )
        self.assertNotIn("PRIVATE METADATA", self.recovery.read_text())
        self.guarded()
        with patch(
            "creator_loop.corrupt_copy_recovery.run_health_check", self.actual_health
        ):
            self.recover()
        require_no_pending_restore(self.root)
        self.assert_kept()

    def test_foreign_pointer_during_health_is_preserved_and_guard_stays(self):
        foreign = {"pointer_format": 1, "status": "FOREIGN", "private": "KEEP THIS"}

        def changed(*args, **kwargs):
            self.pointer.write_text(json.dumps(foreign))
            return readonly_health(self.root)

        with patch("creator_loop.corrupt_copy_recovery.run_health_check", changed):
            with self.assertRaisesRegex(RuntimeError, "pointer changed"):
                self.recover()
        self.assertEqual(json.loads(self.pointer.read_text()), foreign)
        self.guarded()

    def test_original_files_and_candidate_cannot_be_replaced_during_health(self):
        original = json.loads(self.copy.read_text())
        retained = (
            self.root / "backups" / original["retained_directory"] / self.source.name
        )
        executable = self.candidate / "CreatorLoop/CreatorLoop.exe"

        def health(*args, **kwargs):
            for path in (retained, self.source, executable):
                with self.assertRaises(OSError):
                    path.rename(path.with_name(path.name + ".foreign"))
            with self.assertRaises(OSError):
                retained.write_bytes(b"overwrite")
            return self.actual_health(*args, **kwargs)

        with patch("creator_loop.corrupt_copy_recovery.run_health_check", health):
            self.recover()
        self.assert_kept()

    def test_actual_process_crashes_recover_from_bytes_with_fresh_health(self):
        phases = [
            "ACTIVATION_STARTED",
            "ACTIVATED_PENDING_HEALTH",
            "COMPLETED",
            "HEALTH_COMPLETED_GUARDED",
            "RESTORE_COMPLETED",
        ]
        for index, phase in enumerate(phases):
            with self.subTest(phase=phase):
                if index:
                    self.setUp()
                code = """
import os,sys
from pathlib import Path
import creator_loop.corrupt_copy_recovery as m
from creator_loop.update_health import readonly_health
original=m._journal
def crash(path,record):
    original(path,record)
    if record.get('phase')==sys.argv[5]: os._exit(37)
m._journal=crash
m.run_health_check=lambda executable,root,logs,**kwargs: readonly_health(root)
m.recover_corrupt_copy(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),reviewed_inspection=sys.argv[4],confirm_recovery=True)
"""
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(self.root),
                        str(self.copy),
                        str(self.installation),
                        self.token,
                        phase,
                    ],
                    env={**os.environ, "PYTHONPATH": "app"},
                    capture_output=True,
                    timeout=40,
                )
                self.assertEqual(result.returncode, 37, result.stderr)
                self.assert_kept()

                if phase == "RESTORE_COMPLETED":
                    require_no_pending_restore(self.root)
                else:
                    self.guarded()
                    self.token = inspect_corrupt_copy(self.root, self.copy)[
                        "inspection_identity"
                    ]
                    with patch(
                        "creator_loop.corrupt_copy_recovery.run_health_check",
                        self.actual_health,
                    ):
                        self.recover()
                    require_no_pending_restore(self.root)
                self.assert_kept()

    def test_new_foreign_sidecar_immediately_before_guard_clear_keeps_guard(self):
        from creator_loop.update_preparation import _journal

        foreign = self.root / "creator_loop.sqlite3-shm"

        def change(path, record):
            _journal(path, record)
            if record.get("phase") == "HEALTH_COMPLETED_GUARDED":
                foreign.write_bytes(b"KEEP FOREIGN SHM")

        with (
            patch(
                "creator_loop.corrupt_copy_recovery.run_health_check",
                self.actual_health,
            ),
            patch("creator_loop.corrupt_copy_recovery._journal", change),
        ):
            with self.assertRaises(RuntimeError):
                self.recover()
        self.assertEqual(foreign.read_bytes(), b"KEEP FOREIGN SHM")
        self.guarded()

    def test_raw_archive_changed_in_metadata_gap_refuses_guard_clear(self):
        from creator_loop.update_metadata import record_successful_update

        raw = self.fixture.fixture.raw_manifest.parent / "creator_loop.sqlite3"

        def metadata(*args):
            result = record_successful_update(*args)
            raw.write_bytes(b"KEEP FOREIGN RAW")
            return result

        with (
            patch(
                "creator_loop.corrupt_copy_recovery.run_health_check",
                self.actual_health,
            ),
            patch(
                "creator_loop.corrupt_copy_recovery.record_successful_update", metadata
            ),
        ):
            with self.assertRaises(ValueError):
                self.recover()
        self.assertEqual(raw.read_bytes(), b"KEEP FOREIGN RAW")
        with self.assertRaises(PendingRestore):
            require_no_pending_restore(self.root)
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertEqual(self.copy.read_bytes(), self.copy_bytes)

    def test_empty_or_missing_live_database_never_initialized_in_recovery(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.source.unlink()  # our isolated test fixture only
                else:
                    self.source.write_bytes(b"")
                proof = inspect_corrupt_copy(self.root, self.copy)
                with patch(
                    "creator_loop.corrupt_copy_recovery.run_health_check"
                ) as health:
                    with self.assertRaisesRegex(RuntimeError, "Unconfirmed copy"):
                        self.recover(reviewed_inspection=proof["inspection_identity"])
                    health.assert_not_called()
                self.assertEqual(self.source.exists(), not missing)
                if not missing:
                    self.assertEqual(self.source.read_bytes(), b"")
                with self.assertRaises(PendingRestore):
                    require_no_pending_restore(self.root)

    def test_actual_cli_missing_consent_mixed_stale_and_busy_refuse_without_mutation(
        self,
    ):
        base = [
            sys.executable,
            "-m",
            "creator_loop",
            "--recover-corrupt-copy",
            str(self.copy),
            "--reviewed-inspection",
            self.token,
            "--installation-root",
            str(self.installation),
        ]
        environment = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }
        for orphan in (["--confirm-recovery"], ["--reviewed-inspection", self.token]):
            result = subprocess.run(
                [sys.executable, "-m", "creator_loop", *orphan],
                env=environment,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
        for extra in ([], ["--confirm-recovery", "--smoke"]):
            result = subprocess.run(
                base + extra, env=environment, capture_output=True, timeout=20
            )
            self.assertEqual(result.returncode, 2, result.stderr)
        stale = ["0" * 64 if value == self.token else value for value in base]
        result = subprocess.run(
            stale + ["--confirm-recovery"],
            env=environment,
            capture_output=True,
            timeout=25,
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn(b"PRIVATE", result.stderr)
        with AppDataLock(self.root):
            busy = subprocess.run(
                base + ["--confirm-recovery"],
                env=environment,
                capture_output=True,
                timeout=20,
            )
        self.assertEqual(busy.returncode, 3, busy.stderr)
        self.assertFalse(self.pointer.exists())
        self.guarded()

    def test_duplicate_guard_metadata_in_final_gap_is_preserved_not_cleared(self):
        from creator_loop.update_metadata import record_successful_update

        marker = self.root / "runtime/restore-in-progress.json"
        original = marker.read_bytes()

        def metadata(*args):
            result = record_successful_update(*args)
            body = json.loads(original)
            marker.write_bytes(
                original.rstrip()[:-1]
                + b',"copy_id":'
                + json.dumps(body["copy_id"]).encode()
                + b"}"
            )
            return result

        with (
            patch(
                "creator_loop.corrupt_copy_recovery.run_health_check",
                self.actual_health,
            ),
            patch(
                "creator_loop.corrupt_copy_recovery.record_successful_update", metadata
            ),
        ):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.recover()
        self.assertIn(b',"copy_id":', marker.read_bytes())
        self.guarded()

    def test_failed_health_selects_only_schema_compatible_verified_previous(self):
        for compatible in (True, False):
            with self.subTest(compatible=compatible):
                if not compatible:
                    self.setUp()
                previous = self.installation / (
                    self.candidate.name.rsplit("-", 1)[0] + "-" + uuid4().hex
                )
                shutil.copytree(self.candidate, previous)
                manifest = previous / "release-manifest.json"
                if not compatible:
                    body = json.loads(manifest.read_text())
                    body.update(schema_to=5, schema_read_max=5, schema_read_min=1)
                    manifest.write_text(json.dumps(body))
                pointer = {
                    "pointer_format": 1,
                    "status": "ACTIVE",
                    "candidate_name": previous.name,
                    "manifest_sha256": _digest(manifest),
                    "update_id": "previous",
                }
                self.pointer.write_text(json.dumps(pointer))
                with patch(
                    "creator_loop.corrupt_copy_recovery.run_health_check",
                    side_effect=RuntimeError("PRIVATE FAILURE"),
                ):
                    with self.assertRaises(RuntimeError):
                        self.recover()
                self.assertTrue(previous.is_dir())
                if compatible:
                    self.assertEqual(json.loads(self.pointer.read_text()), pointer)
                else:
                    self.assertEqual(
                        json.loads(self.pointer.read_text())["status"], "HEALTH_FAILED"
                    )
                self.guarded()

    def test_success_metadata_preserves_root_and_component_ownership(self):
        registry = self.root / "manifests/storage-roots.json"
        original = {
            "manifest_version": 1,
            "data_root_id": uuid4().hex,
            "schema_version": 6,
            "storage_roots": [],
            "component_installations": [{"fixture": "unowned-external-component"}],
        }
        registry.write_text(json.dumps(original))
        with patch(
            "creator_loop.corrupt_copy_recovery.run_health_check", self.actual_health
        ):
            self.recover()
        result = json.loads(registry.read_text())
        for key in ("data_root_id", "storage_roots", "component_installations"):
            self.assertEqual(result[key], original[key])
        self.assertEqual(
            result["last_backup_id"], json.loads(self.copy.read_text())["backup_id"]
        )
        self.assert_kept()
