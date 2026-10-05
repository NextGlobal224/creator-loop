"""Real owned fresh CLI through Qt; never reuse interrupted or health consent."""

import copy
import hashlib
import json
import os
import time
import unittest

import test_corrupt_restore_copy as copy_fixture
from creator_loop.restore_assessment import assessment_identity

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from creator_loop.fresh_restore_ui import FreshRestoreWindow
    from creator_loop.maintenance_ui import MaintenanceWindow
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(os.name == "nt", "Real Qt/owned Windows fresh restore")
class FreshRestoreUITests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.fixture.fixture.installation
        self.candidate = self.fixture.fixture.candidate
        self.backup = self.fixture.fixture.backup_id
        self.journal = self.fixture.copy()
        self.window = FreshRestoreWindow(
            self.root,
            str(self.installation),
            str(self.candidate),
            self.backup,
            str(self.journal),
        )
        self.addCleanup(self.window.close)

    def unknown(self, sidecars=False):
        current = {"creator_loop.sqlite3": b"PRIVATE CURRENT UNKNOWN"}
        if sidecars:
            current.update(
                {
                    "creator_loop.sqlite3" + suffix: b"PRIVATE CURRENT "
                    + suffix.encode()
                    for suffix in ("-wal", "-shm", "-journal")
                }
            )
        for name, payload in current.items():
            (self.root / name).write_bytes(payload)
        return {
            name: (
                payload,
                (self.root / name).stat().st_dev,
                (self.root / name).stat().st_ino,
            )
            for name, payload in current.items()
        }

    def wait(self, window):
        deadline = time.monotonic() + 30
        while window.command.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(window.command.busy, window.status.text())
        self.app.processEvents()

    def review(self, window=None):
        window = window or self.window
        window.review_button.click()
        self.wait(window)
        self.assertIsNotNone(window.proof, window.status.text())
        return copy.deepcopy(window.proof)

    def protected(self):
        return {
            path: path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and "logs" not in path.relative_to(self.root).parts
        }

    def assert_protected(self, expected, *, copied=False):
        for path, payload in expected.items():
            if copied and (
                path.parent == self.root or path.name == "restore-in-progress.json"
            ):
                continue
            self.assertEqual(path.read_bytes(), payload, str(path))

    def parent(self):
        parent = MaintenanceWindow(self.root, self.installation)
        parent.candidate.setText(str(self.candidate))
        parent.backup_id.setText(self.backup)
        parent.journal.setText(str(self.journal))
        self.addCleanup(parent.close)
        return parent

    def deliver(self, proof):
        self.window.operation = "review"
        self.window.pending_selection = self.window._selection()
        self.window._finished(0, json.dumps(proof), "")

    def rehash(self, choice):
        inspection = choice["copy_review"]["inspection"]
        inspection["inspection_identity"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in inspection.items()
                    if key != "inspection_identity"
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        choice["copy_review"]["assessment_identity"] = assessment_identity(
            choice["copy_review"]
        )
        choice["assessment_identity"] = assessment_identity(choice)

    def test_real_parent_fresh_copy_retains_four_native_files_then_needs_new_health_review(
        self,
    ):
        parent = self.parent()
        parent.buttons["review-corrupt-copy"].click()
        self.wait(parent)
        self.assertEqual(
            parent.copy_review["inspection"]["actual_state"], "VALIDATED_COPY_GUARDED"
        )
        parent.health_consent.setChecked(True)
        original_selection = parent._selection()
        current = self.unknown(sidecars=True)
        before = self.protected()
        original_journal = self.journal.read_bytes()
        parent.buttons["fresh-completed-restore"].click()
        dialog = parent.fresh_window
        self.assertIsInstance(dialog, FreshRestoreWindow)
        self.assertIsNone(parent.copy_review)
        self.assertFalse(parent.health_consent.isChecked())
        self.review(dialog)
        self.assertIn(dialog.proof["backup_created_at"], dialog.review.toPlainText())
        self.assertFalse(dialog.copy_button.isEnabled())
        dialog.loss_consent.setChecked(True)
        self.assertFalse(dialog.copy_button.isEnabled())
        dialog.fresh_consent.setChecked(True)
        self.assertTrue(dialog.copy_button.isEnabled())
        parent.health_consent.setChecked(True)  # Cannot carry old consent across copy.
        dialog.copy_button.click()
        self.wait(dialog)
        self.assertFalse(dialog.isVisible(), dialog.status.text())
        self.assertIsNone(parent.copy_review)
        self.assertFalse(parent.health_consent.isChecked())
        self.assertEqual(parent._selection(), original_selection)
        self.assertEqual(self.journal.read_bytes(), original_journal)
        fresh_paths = list(
            (self.root / "manifests").glob("corrupt-fresh-restore-*.json")
        )
        self.assertEqual(len(fresh_paths), 1, dialog.status.text())
        record = json.loads(fresh_paths[0].read_text())
        retained = self.root / "backups" / record["retained_partial_directory"]
        self.assertEqual({path.name for path in retained.iterdir()}, set(current))
        for name, (payload, device, inode) in current.items():
            path = retained / name
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual((path.stat().st_dev, path.stat().st_ino), (device, inode))
        original_guard = before[self.root / "runtime/restore-in-progress.json"]
        self.assertEqual(
            (
                self.root / "backups" / f"corrupt-guard-{record['copy_id']}.json"
            ).read_bytes(),
            original_guard,
        )
        self.assert_protected(before, copied=True)
        parent.buttons["review-corrupt-copy"].click()
        self.wait(parent)
        self.assertEqual(
            parent.copy_review["inspection"]["actual_state"], "VALIDATED_COPY_GUARDED"
        )
        self.assertEqual(
            len(parent.copy_review["inspection"]["fresh_retention_history"]), 1
        )
        self.assertFalse(parent.buttons["recover-corrupt-copy"].isEnabled())
        self.assertFalse(parent.health_consent.isChecked())
        self.assertTrue((self.root / "runtime/restore-in-progress.json").exists())
        self.assertFalse(record["restored"])

    def test_actual_review_default_consent_and_each_selector_invalidate_proof(self):
        self.unknown()
        good = self.review()
        self.assertIn(good["backup_id"], self.window.review.toPlainText())
        for checkbox in (
            self.window.fresh_consent,
            self.window.loss_consent,
            self.window.media_consent,
        ):
            self.assertFalse(checkbox.isChecked())
        for field in self.window.fields:
            self.deliver(good)
            self.window.fresh_consent.setChecked(True)
            self.window.loss_consent.setChecked(True)
            original = field.text()
            field.setText(original + ".different")
            self.assertIsNone(self.window.proof)
            self.assertFalse(self.window.copy_button.isEnabled())
            self.assertFalse(self.window.fresh_consent.isChecked())
            self.assertFalse(self.window.loss_consent.isChecked())
            field.setText(original)

    def test_malformed_rehashed_native_media_flags_and_stale_response_fail_closed(self):
        self.unknown()
        good = self.review()
        before = self.protected()
        edits = [
            lambda p: p.update(fresh_completed_restore_review_format=True),
            lambda p: p.update(apply_authorized=True),
            lambda p: p.pop("current_counts"),
            lambda p: p.update(completed_database_sha256="z" * 64),
            lambda p: p["current_source_files"][0].update(inode=True),
            lambda p: p["copy_review"]["inspection"]["current_files"][0].update(
                present=1
            ),
            lambda p: p["copy_review"].update(backup_schema=True),
            lambda p: p["copy_review"].update(media_issue_count=False),
            lambda p: p["copy_review"].update(requires_fresh_restore_decision=False),
            lambda p: p["copy_review"]["media_assessment"][0].update(
                expected_size=True
            ),
            lambda p: p["copy_review"]["media_assessment"][0].pop("actual_sha256"),
        ]
        for edit in edits:
            with self.subTest(edit=edits.index(edit)):
                bad = copy.deepcopy(good)
                edit(bad)
                self.rehash(bad)
                self.deliver(bad)
                self.assertIsNone(self.window.proof)
                self.assertFalse(self.window.copy_button.isEnabled())
        self.window.operation = "review"
        self.window.pending_selection = ("old",)
        self.window._finished(0, json.dumps(good), "")
        self.assertIsNone(self.window.proof)
        for bad_text in ("[]", "not json"):
            self.window.operation = "review"
            self.window.pending_selection = self.window._selection()
            self.window._finished(0, bad_text, "")
            self.assertIsNone(self.window.proof)
        self.assert_protected(before)

    def test_missing_media_needs_distinct_ack_and_keeps_health_disabled(self):
        self.unknown()
        media = self.root / "storage/originals/text.txt"
        media.unlink()  # Closed private fixture only; never recreated by copy.
        parent = self.parent()
        parent.buttons["fresh-completed-restore"].click()
        dialog = parent.fresh_window
        self.review(dialog)
        self.assertGreater(dialog.proof["media_issue_count"], 0)
        dialog.fresh_consent.setChecked(True)
        dialog.loss_consent.setChecked(True)
        self.assertFalse(dialog.copy_button.isEnabled())
        dialog.media_consent.setChecked(True)
        self.assertTrue(dialog.copy_button.isEnabled())
        dialog.copy_button.click()
        self.wait(dialog)
        self.assertFalse(dialog.isVisible(), dialog.status.text())
        self.assertFalse(media.exists())
        parent.buttons["review-corrupt-copy"].click()
        self.wait(parent)
        self.assertGreater(parent.copy_review["media_issue_count"], 0)
        parent.health_consent.setChecked(True)
        self.assertFalse(parent.buttons["recover-corrupt-copy"].isEnabled())
        self.assertTrue((self.root / "runtime/restore-in-progress.json").exists())

    def test_actual_same_bytes_new_inode_refuses_stale_fresh_review_before_write(self):
        self.unknown()
        self.review()
        source = self.root / "creator_loop.sqlite3"
        payload = source.read_bytes()
        source.rename(self.root / "private preserved current before stale test")
        source.write_bytes(payload)
        before = self.protected()
        self.window.fresh_consent.setChecked(True)
        self.window.loss_consent.setChecked(True)
        self.window.copy_button.click()
        self.wait(self.window)
        self.assertIsNone(self.window.proof)
        self.assertIn("exit 4", self.window.status.text())
        self.assertEqual(
            list((self.root / "manifests").glob("corrupt-fresh-restore-*.json")), []
        )
        self.assert_protected(before)

    def test_parent_close_terminates_only_held_child_command_and_reopen_requires_review(
        self,
    ):
        self.unknown()
        before = self.protected()
        parent = self.parent()
        parent.buttons["fresh-completed-restore"].click()
        dialog = parent.fresh_window
        dialog.review_button.click()
        process = dialog.command.process
        self.assertIsNotNone(process)
        parent.close()
        self.app.processEvents()
        self.assertIsNone(dialog.command.process)
        self.assertIsNone(process.job)
        self.assertIsNone(process.process)
        self.assertIsNone(dialog.proof)
        self.assertFalse(dialog.fresh_consent.isChecked())
        self.assert_protected(before)
        parent.candidate.clear()
        parent.backup_id.clear()
        parent.buttons["fresh-completed-restore"].click()
        self.assertEqual(dialog.candidate.text(), "")
        self.assertEqual(dialog.backup_id.text(), "")
        self.assertFalse(dialog.review_button.isEnabled())
        self.assertFalse(dialog.copy_button.isEnabled())

    def test_foreign_receipt_cannot_handoff_or_claim_guard_clear(self):
        self.unknown()
        before = self.protected()
        outputs = []
        self.window.copy_ready.connect(lambda *values: outputs.append(values))
        for name, flag in [
            ("../foreign.json", True),
            ("corrupt-fresh-restore-" + "f" * 32 + "-" + "a" * 32 + ".json", True),
            (
                "corrupt-fresh-restore-"
                + self.journal.stem.removeprefix("corrupt-restore-")
                + "-"
                + "a" * 32
                + ".json",
                False,
            ),
        ]:
            self.window.operation = "copy"
            self.window.pending_selection = self.window._selection()
            self.window._finished(
                0,
                json.dumps(
                    {
                        "phase": "FRESH_DB_COMMITTED_GUARDED",
                        "journal_name": name,
                        "activated": False,
                        "restored": False,
                        "guard_retained": flag,
                    }
                ),
                "",
            )
            self.assertIsNone(self.window.proof)
            self.assertFalse(self.window.copy_button.isEnabled())
        self.assertFalse(outputs)
        self.assert_protected(before)
