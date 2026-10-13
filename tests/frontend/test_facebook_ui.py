"""Real Qt regression: preserve QDialog APIs while providing source actions."""

import tempfile
import unittest
from pathlib import Path

from creator_loop.backend.facebook.facebook_research import candidate
from creator_loop.backend.runtime.database import initialize
from creator_loop.frontend.facebook_ui import FacebookDialog
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from tests.backend.test_facebook_research import observation, settings


class FacebookUiTests(unittest.TestCase):
    def test_historical_assessment_is_preserved_but_not_counted_as_current_viral(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            dialog = FacebookDialog(root)
            try:
                params = settings(mode="VIRAL", topic="nhận thức đúng về sự thật")
                item = candidate(
                    observation(
                        excerpt="Nhận lương, kiến thức chuyên môn, công việc đúng và sự thật thà.",
                        metrics={"shares": "295 shares"},
                    ),
                    "nhận thức",
                    params,
                )
                old = {
                    **item["assessment"],
                    "topical": True,
                    "state": "QUALIFIED",
                    "method": "LOCAL_RULES_V1_2",
                }
                row = {
                    **item,
                    "assessment": old,
                    "criteria": params,
                    "candidate_id": "legacy",
                    "search_id": "legacy-search",
                    "saved": False,
                }
                dialog.current_search = "legacy-search"
                dialog.reload_candidates([row])
                self.assertIn("Đạt viral 0", dialog.counts_label.text())
                self.assertEqual(dialog.candidate_rows[0]["assessment_stored"], old)
                self.assertFalse(dialog.candidate_rows[0]["assessment"]["topical"])
                self.assertEqual(row["assessment"], old)
            finally:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()

    def test_viral_counts_separate_analysis_and_wait_for_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            dialog = FacebookDialog(root)
            try:
                params = settings(mode="VIRAL")
                rows = [
                    {
                        **candidate(raw, "Python", params),
                        "candidate_id": str(n),
                        "search_id": "viral-a",
                        "saved": False,
                        "criteria": params,
                    }
                    for n, raw in enumerate(
                        (
                            observation("12", metrics={"likes": "1500 likes"}),
                            observation("13"),
                        )
                    )
                ]
                dialog.current_search = "viral-a"
                dialog.reload_candidates(rows)
                self.assertIn("Ứng viên 2", dialog.counts_label.text())
                self.assertIn("Đạt viral 1", dialog.counts_label.text())
                self.assertIn("Đang chọn lưu 0", dialog.counts_label.text())
                self.assertEqual(dialog.candidate_table.item(0, 1).text(), "Đạt viral")
                self.assertEqual(
                    dialog.candidate_table.item(1, 1).text(), "Chưa đủ căn cứ viral"
                )
                dialog.candidate_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
                self.assertIn("Đang chọn lưu 1", dialog.counts_label.text())
                dialog.research_mode.setCurrentIndex(
                    dialog.research_mode.findData("ANALYSIS")
                )
                self.assertIn("không đồng nghĩa đạt viral", dialog.research_rule.text())
            finally:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()

    def test_unchecked_candidate_stays_unchecked_after_save_and_history_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            dialog = FacebookDialog(root)
            try:
                rows = [
                    {
                        **candidate(observation(str(n)), "Python", settings()),
                        "candidate_id": str(n),
                        "search_id": "search-a",
                        "saved": False,
                        "criteria": settings(),
                    }
                    for n in (12, 13)
                ]
                dialog.current_search = "search-a"
                dialog.reload_candidates(rows)
                self.assertEqual(
                    dialog.candidate_table.item(0, 0).checkState(),
                    Qt.CheckState.Checked,
                )
                dialog.candidate_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
                rows[0]["saved"] = True
                dialog.reload_candidates(rows)
                self.assertEqual(
                    dialog.candidate_table.item(0, 0).checkState(),
                    Qt.CheckState.Unchecked,
                )
                self.assertEqual(
                    dialog.candidate_table.item(1, 0).checkState(),
                    Qt.CheckState.Unchecked,
                )
                dialog.current_search = "search-b"
                dialog.reload_candidates(rows)
                dialog.current_search = "search-a"
                dialog.reload_candidates(rows)
                self.assertEqual(
                    dialog.candidate_table.item(1, 0).checkState(),
                    Qt.CheckState.Unchecked,
                )
                self.assertTrue(rows[0]["saved"])
                self.assertFalse(rows[1]["saved"])
            finally:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_keeps_native_open_method_and_bounded_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            dialog = FacebookDialog(root)
            try:
                self.assertTrue(callable(dialog.open))
                self.assertIsInstance(dialog.open_source, QPushButton)
                self.assertEqual(dialog.open_source.objectName(), "facebookOpen")
                self.assertEqual(dialog.limit.value(), 80)
                self.assertEqual(dialog.research_mode.currentData(), "VIRAL")
                self.assertIn("ĐÚNG CHỦ ĐỀ VÀ ĐẠT VIRAL", dialog.research_rule.text())
                self.assertEqual(dialog.keyword.maxLength(), 200)
                self.assertFalse(dialog.stop.isEnabled())
                dialog.open_source.click()
                self.assertEqual(dialog.status.text(), "Chọn một nguồn đã lưu.")
                dialog.scan.click()
                self.assertEqual(dialog.status.text(), "Nhập từ khóa trước khi Quét.")
                self.assertIsNone(dialog.worker)
            finally:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()

    def test_existing_chrome_scan_requires_a_verified_selected_tab(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            dialog = FacebookDialog(root)
            try:
                self.assertEqual(dialog.browser_mode.currentData(), "existing")
                self.assertEqual(dialog.login.text(), "Kết nối Chrome đang mở")
                dialog.keyword.setText("fixture")
                dialog.scan.click()
                self.assertIsNone(dialog.worker)
                self.assertIn("Kết nối đúng tab Facebook", dialog.status.text())
                dialog.chrome_tabs.clear()
                dialog.chrome_tabs.addItem("selected", "target")
                dialog.verified_target = "target"
                dialog.chrome_tabs.addItem("another", "another")
                dialog.chrome_tabs.setCurrentIndex(1)
                self.assertIsNone(dialog.verified_target)
                dialog.browser_mode.setCurrentIndex(1)
                self.assertEqual(dialog.login.text(), "Đăng nhập riêng")
                self.assertIsNone(dialog.chrome_tabs.currentData())
            finally:
                dialog.close()
                dialog.deleteLater()
                self.app.processEvents()
