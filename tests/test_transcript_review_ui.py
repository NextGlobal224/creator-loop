"""Actual RAW task/UI selection, SQL lineage, source preview and Library handoff."""

import json
import os
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from unittest.mock import patch

if sys.platform == "win32":
    import test_transcript_evidence as fixture
    from creator_loop import __main__
    from creator_loop.app_lock import AppDataLock, DataRootBusy
    from creator_loop.library_ui import LibraryWindow
    from creator_loop.qt_table_items import table_item
    from creator_loop.transcript_review_ui import TranscriptReviewWindow
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtMultimedia import QAudioBufferOutput
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(sys.platform == "win32", "actual Windows Qt private RAW review")
class TranscriptReviewUiTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self.fixture = fixture.TranscriptEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.window = TranscriptReviewWindow(
            self.root, self.fixture.raw_id, "<b>VIDEO_á中</b>"
        )
        self.window.show()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.window.command.shutdown()
        if self.window.preview is not None:
            self.window.preview.segment.player._shutdown()
            self.window.preview.reject()
        self.window.close()

    def until(self, predicate, timeout=15):
        deadline = time.perf_counter() + timeout
        while not predicate() and time.perf_counter() < deadline:
            self.app.processEvents()
            time.sleep(0.003)
        self.assertTrue(predicate(), self.window.status.text())

    def load(self):
        self.window.load_button.click()
        self.until(lambda: not self.window.command.busy)
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertEqual(self.window.table.selectedItems(), [])
        self.assertIsNone(self.window.selected)
        self.assertFalse(self.window.create_button.isEnabled())

    def select(self):
        self.window.table.selectRow(0)
        self.until(
            lambda: self.window.selected is not None and not self.window.command.busy
        )
        self.assertEqual(self.window.text.toPlainText(), " Lời máy chưa duyệt ")
        self.assertTrue(self.window.create_button.isEnabled())

    def query(self, sql):
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            return db.execute(sql).fetchall()

    def test_no_automatic_selection_or_evidence_explicit_create_is_model_pending(self):
        self.load()
        self.fixture._assert_no_evidence()
        self.select()
        self.fixture._assert_no_evidence()
        before = {k: path.read_bytes() for k, path in self.fixture.paths.items()}
        self.window.create_button.click()
        self.assertFalse(self.window.library_button.isEnabled())
        self.until(lambda: not self.window.command.busy)
        self.assertIn("Đã tạo Evidence MODEL", self.window.status.text())
        self.assertFalse(self.window.create_button.isEnabled())
        self.assertIsNone(self.window.selected)
        self.assertEqual(
            self.query(
                "SELECT producer_type,anchor_file_id,processing_run_id FROM evidence_versions"
            ),
            [("MODEL", self.fixture.original_id, self.fixture.run_id)],
        )
        self.assertEqual(self.query("SELECT COUNT(*) FROM review_events"), [(0,)])
        self.assertEqual(
            before, {k: p.read_bytes() for k, p in self.fixture.paths.items()}
        )
        self.window.library_button.click()
        self.assertTrue(self.window.library_requested)
        with AppDataLock(self.root):
            library = LibraryWindow(self.root)
            self.assertEqual(library.evidence_table.rowCount(), 1)
            self.assertEqual(table_item(library.evidence_table, 0, 3).text(), "PENDING")
            library.close()

    def test_preview_actual_source_pcm_and_close_waits_for_owned_playback_cleanup(self):
        self.load()
        self.select()
        self.window.preview_button.click()
        self.until(lambda: self.window.preview is not None)
        preview = self.window.preview
        self.assertEqual((preview.segment.start_ms, preview.segment.end_ms), (0, 90))
        buffers = []
        output = QAudioBufferOutput(preview)
        preview.segment.player.setAudioBufferOutput(output)
        output.audioBufferReceived.connect(lambda b: buffers.append(b))
        preview.segment.play_button.click()
        self.until(lambda: bool(buffers))
        player = preview.segment.player
        self.assertIsNotNone(player.process)
        with patch.object(player.pipe, "try_close", return_value=False):
            preview.reject()
            self.until(lambda: player.process.tree_finished())
            self.app.processEvents()
            self.assertIs(self.window.preview, preview)
            self.assertTrue(preview.isVisible())
            self.assertFalse(self.window.create_button.isEnabled())
            self.assertIsNotNone(player.held)
        self.until(lambda: self.window.preview is None)
        self.assertIsNone(player.process)
        self.assertIsNone(player.pipe)
        self.assertIsNone(player.held)
        self.fixture._assert_no_evidence()

    def test_close_during_request_cancels_and_keeps_window_until_native_ipc_settles(
        self,
    ):
        self.window.load_button.click()
        self.assertTrue(self.window.command.busy)
        self.window.close()
        self.assertTrue(self.window.close_pending)
        self.assertTrue(self.window.isVisible())
        self.until(lambda: not self.window.command.busy and not self.window.isVisible())
        self.assertIsNone(self.window.command.pipe)
        self.fixture._assert_no_evidence()

    def test_close_during_preview_result_never_opens_a_new_modal(self):
        self.load()
        self.select()
        self.window.preview_button.click()
        self.window.close()
        self.until(lambda: not self.window.command.busy and not self.window.isVisible())
        self.assertIsNone(self.window.preview)

    def test_machine_html_is_displayed_as_exact_plain_text(self):
        self.fixture.raw["transcription"][0]["text"] = (
            " <img src='file:///private'> & <script>ä</script> "
        )
        raw = json.dumps(self.fixture.raw, ensure_ascii=False).encode("utf-8")
        self.fixture._replace_registered(self.fixture.raw_id, raw)
        import hashlib

        self.fixture.provenance["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        self.fixture._replace_registered(
            self.fixture.provenance_id, json.dumps(self.fixture.provenance).encode()
        )
        self.load()
        self.window.table.selectRow(0)
        self.until(
            lambda: self.window.selected is not None and not self.window.command.busy
        )
        self.assertEqual(
            self.window.text.toPlainText(), self.fixture.raw["transcription"][0]["text"]
        )
        self.fixture._assert_no_evidence()

    def test_library_selects_canonical_raw_even_when_first_output_is_provenance(self):
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            db.execute(
                "UPDATE asset_files SET created_at='2000-01-01' WHERE file_id=?",
                (self.fixture.provenance_id,),
            )
            db.commit()
        with AppDataLock(self.root):
            library = LibraryWindow(self.root)
            for row in range(library.run_table.rowCount()):
                if (
                    table_item(library.run_table, row, 1).text()
                    == "AUDIO_TRANSCRIPTION"
                ):
                    self.assertEqual(
                        table_item(library.run_table, row, 3).data(
                            Qt.ItemDataRole.UserRole
                        ),
                        self.fixture.provenance_id,
                    )
                    library.run_table.selectRow(row)
                    library.transcript_review_button.click()
                    break
            self.assertEqual(
                library.transcript_review_selection,
                (self.fixture.raw_id, "Historical fixture"),
            )
            library.close()

    def test_actual_launcher_reuses_qt_instance_and_reacquires_library_lock(self):
        self.window.close()
        self.app.setQuitOnLastWindowClosed(True)
        libraries, review_roots = [], []

        def library_factory(root):
            with self.assertRaises(DataRootBusy):
                with AppDataLock(root):
                    pass  # Library is created while its coordinator owns the lock
            window = LibraryWindow(root)
            libraries.append(window)
            if len(libraries) == 1:

                def choose():
                    window.transcript_review_selection = (
                        self.fixture.raw_id,
                        "Historical fixture",
                    )
                    window.close()

                QTimer.singleShot(0, choose)
            else:
                QTimer.singleShot(0, window.close)
            return window

        def review(root, raw_id, title):
            self.assertIs(QApplication.instance(), self.app)
            self.assertEqual(
                (raw_id, title), (self.fixture.raw_id, "Historical fixture")
            )
            with AppDataLock(root):
                review_roots.append(root)  # old Library lock was released
            return 23

        try:
            with (
                patch.dict(os.environ, {"CREATOR_LOOP_DATA_ROOT": str(self.root)}),
                patch.object(sys, "argv", ["creator_loop"]),
                patch(
                    "creator_loop.library_ui.LibraryWindow", side_effect=library_factory
                ),
                patch(
                    "creator_loop.transcript_review_ui.run_transcript_review",
                    side_effect=review,
                ),
            ):
                self.assertEqual(__main__.main(), 0)
            self.assertEqual(len(libraries), 2)
            self.assertEqual(review_roots, [self.root])
        finally:
            self.app.setQuitOnLastWindowClosed(False)
            for library in libraries:
                library.close()


if __name__ == "__main__":
    unittest.main()
