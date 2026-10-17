from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from creator_loop.frontend.research_creator_ui import ResearchCreatorDialog
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from tests.backend.test_research_pipeline import fixture


@unittest.skipUnless(os.name == "nt", "Owned Qt thread proof requires Windows")
class ResearchCreatorUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.dialog = ResearchCreatorDialog(self.root)
        self.addCleanup(self.close)

    def close(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def wait(self):
        deadline = time.monotonic() + 20
        while self.dialog.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(self.dialog.worker, self.dialog.status.text())
        self.assertFalse(self.dialog.error, self.dialog.error)

    def select(self):
        index = next(
            i
            for i, m in enumerate(self.dialog.material_rows)
            if m.material_id == "source:s"
        )
        self.dialog.material_table.item(index, 0).setCheckState(Qt.CheckState.Checked)

    def test_open_no_task_and_all_materials_unselected_then_manual_versioned_draft(
        self,
    ):
        self.assertIsNone(self.dialog.worker)
        self.assertTrue(
            all(
                self.dialog.material_table.item(i, 0).checkState()
                == Qt.CheckState.Unchecked
                for i in range(self.dialog.material_table.rowCount())
            )
        )
        self.dialog.start("analyze")
        self.assertIn("ít nhất một", self.dialog.status.text())
        self.select()
        self.dialog.start("analyze")
        self.wait()
        wid = self.dialog.workflow_id
        self.assertIn("CAPTION_ONLY", self.dialog.analysis.toPlainText())
        self.assertTrue(self.dialog.draft_button.isEnabled())
        self.dialog.direction.setPlainText("Kiểm tra nguồn trước khi chia sẻ")
        self.dialog.start("draft")
        self.wait()
        first = self.dialog.pipeline.load(wid)["drafts"][0]
        self.dialog.body.insertPlainText("\nGhi chú mới do Creator viết.")
        self.dialog.start("draft")
        self.wait()
        second = self.dialog.pipeline.load(wid)["drafts"][0]
        self.assertNotEqual(first, second)
        self.assertEqual(self.dialog.pipeline.draft(second).version_no, 2)

    def test_automatic_without_llm_refuses_template_and_never_starts_c(self):
        self.select()
        self.dialog.mode.setCurrentIndex(self.dialog.mode.findData("AUTOMATIC"))
        self.dialog.format.setCurrentIndex(self.dialog.format.findData("VIDEO"))
        result = []
        self.dialog.draft_ready.connect(lambda vid, auto: result.append((vid, auto)))
        self.dialog.start("automatic")
        self.assertIsNone(self.dialog.worker)
        self.assertEqual(result, [])
        self.assertIn("không thay bằng template", self.dialog.status.text())
        self.dialog.mode.setCurrentIndex(self.dialog.mode.findData("MANUAL"))
        self.dialog.start("analyze")
        self.wait()
        self.dialog.start("draft")
        self.wait()
        self.assertFalse(result[0][1], "B must never request rendering C")
        wid = self.dialog.workflow_id
        old = self.dialog.pipeline.load(wid)
        self.assertEqual({m["source_id"] for m in old["materials"]}, {"s"})
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.dialog = ResearchCreatorDialog(self.root)
        index = self.dialog.history.findData(wid)
        self.assertGreater(index, 0)
        self.dialog.history.setCurrentIndex(index)
        self.assertEqual(
            self.dialog.body.toPlainText(),
            self.dialog.pipeline.draft(result[0][0]).body_text,
        )
        self.assertTrue(self.dialog.draft_button.isEnabled())
