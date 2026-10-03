"""Actual Qt workers and SQLite measurement history; outer runner owns timeout."""

import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import test_observations as fixtures

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidgetItem
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.observation_ui import ObservationDialog
    from creator_loop.observations import list_observations
    from creator_loop.packages import PublicationRepository
    from creator_loop.publication_ui import PublicationDialog


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class ObservationDialogTests(unittest.TestCase):
    _publish = fixtures.ObservationTests._publish

    def setUp(self):
        fixtures.ObservationTests.setUp(self)
        self.app = QApplication.instance() or QApplication([])
        warnings = patch.object(QMessageBox, "warning")
        warnings.start()
        self.addCleanup(warnings.stop)

    def _dialog(self, publish=True):
        if publish:
            self._publish()
        dialog = ObservationDialog(self.root, "post")
        self.addCleanup(dialog.close)
        return dialog

    def _metric(self, dialog, key="views", value="0", definition="FB-views-v1"):
        dialog.add_metric()
        row = dialog.metrics.rowCount() - 1
        for col, text in ((0, key), (2, value), (4, definition)):
            dialog.metrics.setItem(row, col, QTableWidgetItem(text))

    def _wait(self, dialog):
        deadline = time.monotonic() + 10
        while dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(dialog._worker, "Qt worker did not finish")

    def test_zero_unknown_and_later_measurement_history_are_readonly(self):
        dialog = self._dialog()
        self._metric(dialog)
        self._metric(dialog, key="reach", value="", definition="FB-reach-v1")
        dialog.observed_at.setText("2026-10-03T08:00:00+07:00")
        dialog.save()
        self._wait(dialog)
        history = list_observations(self.db, "post")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].value_for("views"), 0)
        self.assertIsNone(history[0].value_for("reach"))
        self.assertEqual(history[0].metrics[0].raw_value, "0")
        self.assertEqual(dialog.metrics.rowCount(), 1)
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertTrue(dialog.observed_at.isReadOnly())
        self.assertIn("MANUAL", dialog.provenance.text())
        dialog.new_observation()
        self._metric(dialog, value="7", definition="FB-views-v2")
        dialog.save()
        self._wait(dialog)
        self.assertEqual(list_observations(self.db, "post")[0], history[0])
        dialog.history.setCurrentIndex(1)
        self.assertEqual(dialog.metrics.item(0, 2).text(), "0.0")
        self.assertEqual(dialog.metrics.item(0, 4).text(), "FB-views-v1")
        self.assertFalse(dialog.save_button.isEnabled())

    def test_failed_numeric_or_backend_validation_preserves_input(self):
        dialog = self._dialog()
        self._metric(dialog, value="not numeric")
        dialog.save()
        self.assertIsNone(dialog._worker)
        self.assertEqual(dialog.metrics.item(0, 2).text(), "not numeric")
        dialog.metrics.item(0, 2).setText("-1")
        dialog.save()
        self._wait(dialog)
        self.assertEqual(dialog.metrics.item(0, 2).text(), "-1")
        self.assertIn("nonnegative", dialog.status.text())
        self.assertEqual(list_observations(self.db, "post"), ())
        self.assertTrue(dialog.save_button.isEnabled())

    def test_committed_measurement_read_failure_latches_until_real_reload(self):
        dialog = self._dialog()
        self._metric(dialog)
        with patch.object(dialog, "reload", return_value=False):
            dialog.save()
            self._wait(dialog)
        self.assertEqual(len(list_observations(self.db, "post")), 1)
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.new_observation()
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertIn("Đã lưu", dialog.status.text())
        self.assertTrue(dialog.reload())
        self.assertTrue(dialog.save_button.isEnabled())

    def test_worker_capture_and_busy_close_preserve_exact_numeric_payload(self):
        dialog = self._dialog()
        self._metric(dialog, value="5")
        entered, release = threading.Event(), threading.Event()
        original = PublicationRepository.record_manual_observation

        def delayed(repo, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("owned fixture release timed out")
            return original(repo, **kwargs)

        with patch.object(PublicationRepository, "record_manual_observation", delayed):
            dialog.show()
            dialog.save()
            self.assertTrue(entered.wait(2))
            try:
                self.assertFalse(dialog.save_button.isEnabled())
                dialog.metrics.item(0, 2).setText("99")
                dialog.close()
                dialog.reject()
                self.assertTrue(dialog.isVisible())
            finally:
                release.set()
            self._wait(dialog)
        self.assertEqual(list_observations(self.db, "post")[0].value_for("views"), 5)

    def test_pending_blocked_and_publication_opens_exact_published_post(self):
        dialog = self._dialog(publish=False)
        self.assertFalse(dialog.save_button.isEnabled())
        self._publish()
        self.assertTrue(dialog.reload())
        self.assertTrue(dialog.save_button.isEnabled())
        publication = PublicationDialog(self.root)
        self.addCleanup(publication.close)
        self.assertTrue(publication.reload("package", "post"))
        self.assertTrue(publication.observation_button.isEnabled())
        fake = MagicMock()
        with patch(
            "creator_loop.publication_ui.ObservationDialog", return_value=fake
        ) as opened:
            publication.open_observations()
        opened.assert_called_once_with(self.root, "post")
        fake.exec.assert_called_once()
