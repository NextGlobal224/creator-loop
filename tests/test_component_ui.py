"""Review binding and real bounded CLI execution; synthetic artifacts only."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from creator_loop.app_lock import AppDataLock
from creator_loop.component_selection import load_component_selection
from creator_loop.component_ui import ComponentWindow
from creator_loop.database import SCHEMA_VERSION, initialize
from creator_loop.local_components import (
    component_manifest_bytes,
    component_review_fingerprint,
    parse_component_manifest,
)
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.paths import ensure_data_root
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


class ComponentUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve(strict=True)
        self.root = self.folder / "user data"
        ensure_data_root(self.root)
        self.db = self.root / "creator_loop.sqlite3"
        initialize(self.db)
        self.model = self.folder / "unowned fake model.fixture"
        self.payload = b"PRIVATE FAKE MODEL MUST NOT EXECUTE\x00\xff"
        self.model.write_bytes(self.payload)
        self.manifest = self.folder / "selected models.json"
        self.entry = {
            "component_id": "fake-model",
            "kind": "MODEL",
            "path": str(self.model),
            "version": "fake-v1",
            "sha256": hashlib.sha256(self.payload).hexdigest(),
            "byte_size": len(self.payload),
            "source_url": "https://example.invalid/fixture",
            "license": "FAKE TEST ONLY",
            "license_url": "https://example.invalid/license",
            "owned": False,
            "local_use_allowed": True,
            "worker_memory_bytes": 512 * 1024**2,
        }
        self.write_request(self.entry)
        self.user_manifest = self.root / "manifests/storage-roots.json"
        self.baseline = {
            "manifest_version": 1,
            "schema_version": SCHEMA_VERSION,
            "data_root_id": uuid4().hex,
            "storage_roots": [],
            "component_installations": [{"name": "keep external", "owned": False}],
            "last_backup_id": uuid4().hex,
        }
        self.user_manifest.write_text(json.dumps(self.baseline), encoding="utf-8")
        self.window = ComponentWindow(self.root)
        self.addCleanup(self.window.close)
        self.addCleanup(self.window.command.shutdown)
        self.window.manifest.setText(str(self.manifest))

    def write_request(self, entry):
        self.manifest.write_text(
            json.dumps({"manifest_version": 1, "components": [entry]}), encoding="utf-8"
        )

    def until(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate(), self.window.status.text())

    def review(self):
        specs = parse_component_manifest(self.manifest.read_bytes())
        self.window.operation = "check"
        self.window.review_path = str(self.manifest)
        self.window._finished(
            0,
            json.dumps(
                {
                    "check": "LOCAL_ARTIFACTS_VERIFIED",
                    "runtime_compatibility_verified": False,
                    "review_manifest": json.loads(component_manifest_bytes(specs)),
                    "review_fingerprint": component_review_fingerprint(specs),
                }
            ),
        )

    def test_review_requires_explicit_consent_and_input_change_invalidates(self):
        self.assertFalse(self.window.save.isEnabled())
        self.review()
        self.assertIsNotNone(self.window.fingerprint)
        self.assertEqual(self.window.table.item(0, 2).text(), "fake-v1")
        self.assertFalse(self.window.save.isEnabled())
        self.window.consent.setChecked(True)
        self.assertTrue(self.window.save.isEnabled())
        self.window.manifest.setText(str(self.folder / "different.json"))
        self.assertFalse(self.window.consent.isChecked())
        self.assertFalse(self.window.save.isEnabled())
        self.assertIsNone(self.window.fingerprint)
        self.assertEqual(self.window.table.rowCount(), 0)

    def test_malformed_or_runtime_claimed_result_never_enables_save(self):
        self.review()
        self.window.consent.setChecked(True)
        self.window._finished(0, '{"runtime_compatibility_verified":true}')
        self.assertFalse(self.window.save.isEnabled())
        self.assertIsNone(self.window.fingerprint)
        self.assertFalse(self.window.consent.isChecked())

    def test_history_display_never_grants_fresh_review_or_consent(self):
        self.review()
        self.window.consent.setChecked(True)
        self.window._invalidate()
        self.window.operation = "inspect"
        specs = parse_component_manifest(self.manifest.read_bytes())
        self.window._finished(
            0,
            json.dumps(
                {
                    "selection": "HISTORY",
                    "freshly_verified": False,
                    "runtime_compatibility_verified": False,
                    "checked_at": "2026-10-04T00:00:00+00:00",
                    "manifest": json.loads(component_manifest_bytes(specs)),
                }
            ),
        )
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertIn("Chưa kiểm lại", self.window.status.text())
        self.assertFalse(self.window.save.isEnabled())
        self.assertFalse(self.window.consent.isChecked())

    @unittest.skipUnless(
        sys.platform == "win32", "actual historical inspection with offline model"
    )
    def test_actual_inspect_offline_saved_model_is_history_not_available_runtime(self):
        from creator_loop.component_selection import save_component_selection

        specs = parse_component_manifest(self.manifest.read_bytes())
        with AppDataLock(self.root) as lock:
            save_component_selection(self.root, specs, lock=lock)
        self.model.unlink()  # own fake fixture; simulate missing external artifact
        self.window._inspect()
        self.until(lambda: not self.window.command.busy)
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertFalse(self.window.save.isEnabled())
        self.assertIsNone(self.window.fingerprint)
        self.assertIn("Chưa kiểm lại", self.window.status.text())
        self.assertFalse(self.model.exists())

    def test_invalid_non_object_error_result_clears_review(self):
        self.review()
        self.window.consent.setChecked(True)
        self.window._finished(1, "[]")
        self.assertFalse(self.window.save.isEnabled())
        self.assertIsNone(self.window.fingerprint)

    def test_source_component_window_smoke_does_not_initialize_or_migrate_db(self):
        env = {
            **os.environ,
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "app"),
        }
        before = self.db.read_bytes()
        result = subprocess.run(
            [sys.executable, "-m", "creator_loop", "--components", "--ui-smoke"],
            env=env,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(
            json.loads(self.user_manifest.read_text(encoding="utf-8")), self.baseline
        )

    def test_actual_library_transition_releases_app_lock_before_component_window(self):
        script = """
import os
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from creator_loop import __main__ as launcher, library_ui, component_ui
from creator_loop.app_lock import AppDataLock
old_library = library_ui.LibraryWindow
old_components = component_ui.run_components
opened = []
def library(root):
    window = old_library(root)
    QTimer.singleShot(100, window.components_button.click)
    return window
def components(root, **kwargs):
    with AppDataLock(root):
        opened.append(True)
    return old_components(root, ui_smoke=True)
with patch.object(library_ui, "LibraryWindow", side_effect=library):
    with patch.object(component_ui, "run_components", side_effect=components):
        result = launcher.main()
assert result == 0 and opened == [True], (result, opened)
print("Library close/app lock release/component window PASS")
"""
        env = {
            **os.environ,
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "app"),
        }
        result = subprocess.run(
            [sys.executable, "-u", "-c", script],
            env=env,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"component window PASS", result.stdout)
        self.assertEqual(
            json.loads(self.user_manifest.read_text(encoding="utf-8")), self.baseline
        )

    @unittest.skipUnless(sys.platform == "win32", "actual owned Windows GUI command")
    def test_actual_check_save_preserves_domain_registry_and_external_file(self):
        with closing(sqlite3.connect(self.db)) as db:
            before = list(db.iterdump())
        self.window._check()
        self.assertFalse(self.window.manifest.isEnabled())
        self.until(lambda: not self.window.command.busy)
        self.assertIsNotNone(self.window.fingerprint)
        self.assertFalse(self.window.save.isEnabled())
        self.window.consent.setChecked(True)
        self.window._save()
        self.until(lambda: not self.window.command.busy)
        self.assertIn("Đã lưu", self.window.status.text())
        self.assertEqual(
            load_component_selection(self.root).specs[0].sha256, self.entry["sha256"]
        )
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(list(db.iterdump()), before)
        payload = json.loads(self.user_manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                key: value
                for key, value in payload.items()
                if key != "component_selection"
            },
            self.baseline,
        )
        self.assertEqual(self.model.read_bytes(), self.payload)

    @unittest.skipUnless(sys.platform == "win32", "actual stale-review CLI binding")
    def test_declarations_changed_on_disk_after_review_refused_even_if_digest_still_matches(
        self,
    ):
        self.window._check()
        self.until(lambda: not self.window.command.busy)
        self.assertIsNotNone(self.window.fingerprint)
        self.write_request({**self.entry, "version": "fake-change-with-same-bytes"})
        self.window.consent.setChecked(True)
        self.window._save()
        self.until(lambda: not self.window.command.busy)
        self.assertIn("changed after review", self.window.status.text())
        self.assertFalse(self.window.save.isEnabled())
        self.assertEqual(
            json.loads(self.user_manifest.read_text(encoding="utf-8")), self.baseline
        )

    @unittest.skipUnless(sys.platform == "win32", "actual fresh hash after review")
    def test_model_changed_after_review_refused_without_restoring_user_bytes(self):
        self.window._check()
        self.until(lambda: not self.window.command.busy)
        self.model.write_bytes(b"USER CHANGED FILE")
        self.window.consent.setChecked(True)
        self.window._save()
        self.until(lambda: not self.window.command.busy)
        self.assertFalse(self.window.save.isEnabled())
        self.assertEqual(self.model.read_bytes(), b"USER CHANGED FILE")
        self.assertEqual(
            json.loads(self.user_manifest.read_text(encoding="utf-8")), self.baseline
        )

    @unittest.skipUnless(sys.platform == "win32", "actual app lock refusal")
    def test_active_app_refuses_save_and_preserves_reviewed_external_files(self):
        self.window._check()
        self.until(lambda: not self.window.command.busy)
        self.window.consent.setChecked(True)
        with AppDataLock(self.root):
            self.window._save()
            self.until(lambda: not self.window.command.busy)
        self.assertIn("Close the app", self.window.status.text())
        self.assertEqual(
            json.loads(self.user_manifest.read_text(encoding="utf-8")), self.baseline
        )
        self.assertEqual(self.model.read_bytes(), self.payload)

    def fake(self, script):
        def launch(executable, arguments, logs, **kwargs):
            return OwnedWindowsProcess(executable, ["-u", "-c", script], logs, **kwargs)

        return patch(
            "creator_loop.component_command.OwnedWindowsProcess", side_effect=launch
        )

    @unittest.skipUnless(sys.platform == "win32", "actual Job cancel/Qt pulses")
    def test_close_waits_for_native_tree_without_blocking_gui_or_killing_sentinel(self):
        with self.fake("import time;time.sleep(30)"):
            self.window.show()
            self.window._check()
        process = self.window.command.process
        self.assertIsNotNone(process)
        kernel = process.kernel
        handle = kernel.OpenProcess(0x00101000, False, process.record["pid"])
        self.assertTrue(handle)
        self.addCleanup(kernel.CloseHandle, handle)
        sentinel_logs = self.root / "logs/sentinel"
        with OwnedWindowsProcess(
            Path(sys.executable),
            ["-c", "import time;time.sleep(30)"],
            sentinel_logs,
            component_version="unrelated-fixture",
        ) as sentinel:
            started = time.monotonic()
            self.window.close()
            self.assertLess(time.monotonic() - started, 0.2)
            self.until(lambda: not self.window.command.busy)
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0)
            self.assertEqual(
                sentinel.kernel.WaitForSingleObject(sentinel.process, 0), 258
            )
        self.assertEqual(self.model.read_bytes(), self.payload)

    @unittest.skipUnless(sys.platform == "win32", "actual bounded timeout/Qt dispatch")
    def test_deadline_keeps_gui_dispatching_and_retires_owned_worker(self):
        pulses = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: pulses.append(time.monotonic()))
        timer.start()
        self.addCleanup(timer.stop)
        with self.fake("import time;time.sleep(30)"):
            self.window.operation = "check"
            self.window.command.start(
                ["--check-components", str(self.manifest)], timeout_seconds=0.4
            )
        self.until(lambda: not self.window.command.busy)
        self.assertGreater(len(pulses), 20)
        self.assertIn("Hết thời gian", self.window.status.text())
        self.assertEqual(self.model.read_bytes(), self.payload)
