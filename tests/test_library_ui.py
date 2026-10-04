"""Exercise the actual Qt Library import flow on Windows with pinned PySide6."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import initialize

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from creator_loop.audio_evidence_ui import AudioEvidenceDialog, AudioRangeView
        from creator_loop.evidence_review_ui import EvidenceReviewDialog
        from creator_loop.image_evidence_ui import ImageEvidenceDialog, ImageRegionView
        from creator_loop.image_thumbnail import create_image_thumbnail
        from creator_loop.library_ui import LibraryWindow
        from creator_loop.media_intake import (
            intake_image_original,
            intake_video_original,
        )
        from creator_loop.project_ui import ProjectDialog
        from creator_loop.source_ui import SourceDialog
        from creator_loop.text_evidence_ui import TextEvidenceDialog
        from creator_loop.text_intake import intake_text_original
        from creator_loop.video_evidence_ui import VideoEvidenceDialog, VideoRangeView
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtWidgets import (
            QApplication,
            QDialogButtonBox,
            QFileDialog,
        )
    except ImportError:
        QApplication = None
else:
    QApplication = None


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class LibraryUiTests(unittest.TestCase):
    def _wait_for_worker(self, app: QApplication, window: LibraryWindow) -> None:
        deadline = time.monotonic() + 10
        while window._worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(window._worker)

    def test_projects_open_from_library_and_persist_after_dialog_close(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize(root / "creator_loop.sqlite3")
            window = LibraryWindow(root)
            self.addCleanup(window.close)
            project_ids: list[str] = []

            def create_project(dialog: ProjectDialog) -> int:
                self.assertEqual(dialog.root, root)
                dialog.title.setText("Dự án từ Library 📝")
                dialog.create_button.click()
                deadline = time.monotonic() + 10
                while dialog._worker is not None and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                self.assertIsNone(dialog._worker)
                project_ids.append(dialog.selected_project_id() or "")
                dialog.close()
                return ProjectDialog.DialogCode.Rejected

            button = next(b for b in window._buttons if b.text() == "Projects")
            with patch.object(ProjectDialog, "exec", create_project):
                button.click()
            self.assertTrue(project_ids[0])
            reopened = ProjectDialog(root)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.selected_project_id(), project_ids[0])
            self.assertIn("Dự án từ Library", reopened.project.currentText())

    def test_three_original_types_appear_after_worker_import(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu Creator Loop"
        root.mkdir()
        initialize(root / "creator_loop.sqlite3")
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()

        self.assertEqual(window.table.rowCount(), 0)
        from PySide6.QtGui import QImage

        png_path = Path(self.temp.name) / "fixture.png"
        image = QImage(10, 6, QImage.Format.Format_RGB32)
        image.fill(0xFFFF00FF)
        self.assertTrue(image.save(str(png_path)))
        samples = (
            ("TEXT", "Bản ghi Đà Nẵng.txt", "Một đoạn văn".encode("utf-8")),
            (
                "VIDEO",
                "Huế Kha.mp4",
                (
                    Path(__file__).parent / "fixtures" / "video-with-tone.mp4"
                ).read_bytes(),
            ),
            ("IMAGE", "Ảnh mẫu.png", png_path.read_bytes()),
        )
        for index, (kind, name, content) in enumerate(samples, start=1):
            source = Path(self.temp.name) / name
            source.write_bytes(content)
            with patch.object(
                QFileDialog, "getOpenFileName", return_value=(str(source), "")
            ):
                window.choose_original(kind, "Tất cả tệp (*)")
            self.assertIsNotNone(window._worker)
            self.assertTrue(all(not button.isEnabled() for button in window._buttons))

            self._wait_for_worker(app, window)
            self.assertEqual(window.table.rowCount(), index)
            self.assertTrue(all(button.isEnabled() for button in window._buttons))

        shown = {
            (window.table.item(row, 0).text(), window.table.item(row, 1).text())
            for row in range(window.table.rowCount())
        }
        self.assertEqual(shown, {(kind, name) for kind, name, _ in samples})

    def test_create_and_reopen_text_evidence_from_ui(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu Evidence"
        root.mkdir()
        initialize(root / "creator_loop.sqlite3")
        source = Path(self.temp.name) / "Bản gốc.txt"
        source.write_text("Cafe\u0301 ở Huế", encoding="utf-8")
        intake_text_original(source, root=root)
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.table.selectRow(0)

        def accept_cafe(dialog: TextEvidenceDialog) -> object:
            dialog.start.setValue(0)
            dialog.end.setValue(4)
            return TextEvidenceDialog.DialogCode.Accepted

        with patch.object(TextEvidenceDialog, "exec", accept_cafe):
            window.choose_text_evidence()
            self._wait_for_worker(app, window)

        self.assertEqual(window.evidence_table.rowCount(), 1)
        self.assertEqual(window.evidence_table.item(0, 1).text(), "Café")
        window.evidence_table.selectRow(0)
        with patch("creator_loop.library_ui.QMessageBox.information") as shown:
            window.reopen_selected_text_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(shown.call_args.args[2], "Café")

    def test_review_selected_evidence_from_ui_keeps_event_history(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu review UI"
        root.mkdir()
        db_path = root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Bản gốc.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        intake_text_original(source, root=root)
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.table.selectRow(0)

        def select_excerpt(dialog: TextEvidenceDialog) -> object:
            dialog.start.setValue(0)
            dialog.end.setValue(4)
            return TextEvidenceDialog.DialogCode.Accepted

        with patch.object(TextEvidenceDialog, "exec", select_excerpt):
            window.choose_text_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.item(0, 3).text(), "PENDING")
        window.evidence_table.selectRow(0)

        def accept(dialog: EvidenceReviewDialog) -> object:
            self.assertEqual(dialog.action.currentData(), "ACCEPT")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            return EvidenceReviewDialog.DialogCode.Accepted

        with patch.object(EvidenceReviewDialog, "exec", accept):
            window.choose_evidence_review()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.item(0, 3).text(), "ACCEPT")
        window.evidence_table.selectRow(0)

        def reject(dialog: EvidenceReviewDialog) -> object:
            self.assertEqual(dialog.action.currentData(), "ACCEPT")
            dialog.action.setCurrentIndex(1)
            self.assertFalse(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            dialog.reason.setText("Nguồn cần sửa")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            return EvidenceReviewDialog.DialogCode.Accepted

        with patch.object(EvidenceReviewDialog, "exec", reject):
            window.choose_evidence_review()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.item(0, 3).text(), "REJECT")
        with closing(sqlite3.connect(db_path)) as db:
            events = db.execute(
                "SELECT action,actor_id,reason FROM review_events ORDER BY rowid"
            ).fetchall()
        self.assertEqual(
            events,
            [("ACCEPT", "reviewer", None), ("REJECT", "reviewer", "Nguồn cần sửa")],
        )

    def test_text_range_dialog_uses_unicode_code_points(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        dialog = TextEvidenceDialog("😀Café")
        self.addCleanup(dialog.close)
        dialog.start.setValue(1)
        dialog.end.setValue(5)
        app.processEvents()
        self.assertEqual(dialog.preview.text(), "Café")

    def test_correct_text_evidence_from_ui_preserves_both_versions(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu sửa Evidence"
        root.mkdir()
        db_path = root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Bản gốc.txt"
        source.write_text("Café ở Huế", encoding="utf-8")
        intake_text_original(source, root=root)
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.table.selectRow(0)

        def create(dialog: TextEvidenceDialog) -> object:
            dialog.start.setValue(0)
            dialog.end.setValue(4)
            return TextEvidenceDialog.DialogCode.Accepted

        with patch.object(TextEvidenceDialog, "exec", create):
            window.choose_text_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 1)
        old_id = window.evidence_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        window.evidence_table.selectRow(0)

        def correct(dialog: TextEvidenceDialog) -> object:
            self.assertTrue(dialog.correction)
            self.assertEqual((dialog.start.value(), dialog.end.value()), (0, 4))
            self.assertFalse(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            dialog.start.setValue(7)
            dialog.end.setValue(10)
            dialog.reason.setText("Chọn đúng địa danh")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            return TextEvidenceDialog.DialogCode.Accepted

        with patch.object(TextEvidenceDialog, "exec", correct):
            window.correct_selected_text_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 2)
        self.assertIn("0 Claim Version cần xem lại", window.status.text())
        shown: dict[int, str] = {}
        for row in range(2):
            version = int(window.evidence_table.item(row, 2).text())
            window.evidence_table.selectRow(row)
            with patch("creator_loop.library_ui.QMessageBox.information") as popup:
                window.reopen_selected_text_evidence()
                self._wait_for_worker(app, window)
            shown[version] = popup.call_args.args[2]
        self.assertEqual(shown, {1: "Café", 2: "Huế"})
        with closing(sqlite3.connect(db_path)) as db:
            event = db.execute(
                "SELECT evidence_version_id,action,actor_id,reason FROM review_events"
            ).fetchone()
        self.assertEqual(event, (old_id, "CORRECT", "creator", "Chọn đúng địa danh"))

    def test_source_is_attached_and_reused_across_assets(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu Source"
        root.mkdir()
        db_path = root / "creator_loop.sqlite3"
        initialize(db_path)
        for name in ("Một.txt", "Hai.txt"):
            source = Path(self.temp.name) / name
            source.write_text(name, encoding="utf-8")
            intake_text_original(source, root=root)
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.table.selectRow(0)

        def create_web_source(dialog: SourceDialog) -> object:
            dialog.platform.setText("WEB")
            dialog.url.setText("https://example.invalid/post")
            dialog.rights.setCurrentText("REFERENCE_ONLY")
            dialog.relationship.setCurrentText("REFERENCE")
            return SourceDialog.DialogCode.Accepted

        with patch.object(SourceDialog, "exec", create_web_source):
            window.choose_source()
            self._wait_for_worker(app, window)
        with closing(sqlite3.connect(db_path)) as db:
            source_id = db.execute("SELECT source_id FROM sources").fetchone()[0]
        window.table.selectRow(1)

        def attach_existing(dialog: SourceDialog) -> object:
            dialog.mode.setCurrentIndex(1)
            self.assertEqual(dialog.existing.currentData(), source_id)
            dialog.relationship.setCurrentText("REFERENCE")
            return SourceDialog.DialogCode.Accepted

        with patch.object(SourceDialog, "exec", attach_existing):
            window.choose_source()
            self._wait_for_worker(app, window)
        window.table.selectRow(0)

        def add_second_source(dialog: SourceDialog) -> object:
            dialog.platform.setText("LOCAL")
            return SourceDialog.DialogCode.Accepted

        with patch.object(SourceDialog, "exec", add_second_source):
            window.choose_source()
            self._wait_for_worker(app, window)
        with closing(sqlite3.connect(db_path)) as db:
            links = db.execute(
                "SELECT source_id,asset_id FROM source_assets ORDER BY asset_id"
            ).fetchall()
            local_source = db.execute(
                "SELECT canonical_url,external_id,publisher_name,rights_status "
                "FROM sources WHERE platform='LOCAL'"
            ).fetchone()
        self.assertEqual(len(links), 3)
        self.assertEqual(sum(row[0] == source_id for row in links), 2)
        self.assertEqual(local_source, (None, None, None, "UNKNOWN"))
        second_asset_id = window.table.item(1, 1).data(Qt.ItemDataRole.UserRole)
        details = SourceDialog(root, str(second_asset_id), "Hai.txt")
        self.addCleanup(details.close)
        self.assertEqual(details.links.rowCount(), 1)

    def test_create_and_reopen_image_region_from_ui(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu Image"
        root.mkdir()
        initialize(root / "creator_loop.sqlite3")
        source = Path(self.temp.name) / "Ảnh màu.png"
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        for y in range(2, 4):
            for x in range(2, 4):
                image.setPixelColor(x, y, QColor("blue"))
        self.assertTrue(image.save(str(source)))
        intake_image_original(source, root=root)
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.table.selectRow(0)

        def select_blue_region(dialog: ImageEvidenceDialog) -> object:
            dialog.coords["x"].setValue(0.5)
            dialog.coords["y"].setValue(0.5)
            dialog.coords["width"].setValue(0.5)
            dialog.coords["height"].setValue(0.5)
            dialog.content.setText("Góc xanh")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            return ImageEvidenceDialog.DialogCode.Accepted

        with patch.object(ImageEvidenceDialog, "exec", select_blue_region):
            window.choose_image_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 1)
        self.assertEqual(window.evidence_table.item(0, 1).text(), "Góc xanh")
        window.evidence_table.selectRow(0)
        viewed: list[str] = []

        def inspect_region(dialog: ImageRegionView) -> object:
            viewed.append(dialog.region_view.pixmap().toImage().pixelColor(0, 0).name())
            return ImageRegionView.DialogCode.Accepted

        with patch.object(ImageRegionView, "exec", inspect_region):
            window.reopen_selected_image_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(viewed, [QColor("blue").name()])

    def test_create_image_evidence_from_selected_thumbnail_run(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu Evidence thumbnail"
        root.mkdir()
        db_path = root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Ảnh gốc.png"
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        for y in range(2, 4):
            for x in range(2, 4):
                image.setPixelColor(x, y, QColor("blue"))
        self.assertTrue(image.save(str(source)))
        imported = intake_image_original(source, root=root)
        thumbnail = create_image_thumbnail(imported.file_id, data_root=root, max_edge=2)
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.run_table.selectRow(0)

        def select_region(dialog: ImageEvidenceDialog) -> object:
            dialog.coords["x"].setValue(0.5)
            dialog.coords["y"].setValue(0.5)
            dialog.coords["width"].setValue(0.5)
            dialog.coords["height"].setValue(0.5)
            dialog.content.setText("Xanh trên thumbnail")
            return ImageEvidenceDialog.DialogCode.Accepted

        with patch.object(ImageEvidenceDialog, "exec", select_region):
            window.choose_thumbnail_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 1)
        self.assertIn("Image thumbnail", window.evidence_table.item(0, 0).text())
        with closing(sqlite3.connect(db_path)) as db:
            anchor = db.execute(
                "SELECT anchor_file_id FROM evidence_versions"
            ).fetchone()[0]
        self.assertEqual(anchor, thumbnail.file_id)
        window.evidence_table.selectRow(0)
        viewed: list[str] = []

        def inspect_region(dialog: ImageRegionView) -> object:
            viewed.append(dialog.region_view.pixmap().toImage().pixelColor(0, 0).name())
            return ImageRegionView.DialogCode.Accepted

        with patch.object(ImageRegionView, "exec", inspect_region):
            window.reopen_selected_image_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(viewed, [QColor("blue").name()])

    def test_correct_thumbnail_image_evidence_from_ui_preserves_anchor(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu sửa thumbnail"
        root.mkdir()
        db_path = root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Ảnh đỏ xanh.png"
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        for y in range(2, 4):
            for x in range(2, 4):
                image.setPixelColor(x, y, QColor("blue"))
        self.assertTrue(image.save(str(source)))
        imported = intake_image_original(source, root=root)
        thumbnail = create_image_thumbnail(imported.file_id, data_root=root, max_edge=2)
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.run_table.selectRow(0)

        def create(dialog: ImageEvidenceDialog) -> object:
            dialog.coords["x"].setValue(0.5)
            dialog.coords["y"].setValue(0.5)
            dialog.coords["width"].setValue(0.5)
            dialog.coords["height"].setValue(0.5)
            dialog.content.setText("Xanh trên thumbnail")
            return ImageEvidenceDialog.DialogCode.Accepted

        with patch.object(ImageEvidenceDialog, "exec", create):
            window.choose_thumbnail_evidence()
            self._wait_for_worker(app, window)
        old_id = window.evidence_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        window.evidence_table.selectRow(0)

        def correct(dialog: ImageEvidenceDialog) -> object:
            self.assertTrue(dialog.correction)
            self.assertEqual(dialog.content.text(), "Xanh trên thumbnail")
            self.assertEqual(dialog.region()["x"], 0.5)
            self.assertFalse(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            dialog.coords["x"].setValue(0.0)
            dialog.coords["y"].setValue(0.0)
            dialog.content.setText("Đỏ trên thumbnail")
            dialog.reason.setText("Sửa vùng quan sát")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            return ImageEvidenceDialog.DialogCode.Accepted

        with patch.object(ImageEvidenceDialog, "exec", correct):
            window.correct_selected_image_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 2)
        self.assertIn("0 Claim Version cần xem lại", window.status.text())
        with closing(sqlite3.connect(db_path)) as db:
            versions = db.execute(
                """SELECT evidence_version_id,version_no,anchor_file_id,content
                   FROM evidence_versions ORDER BY version_no"""
            ).fetchall()
            event = db.execute(
                "SELECT evidence_version_id,action,reason FROM review_events"
            ).fetchone()
        self.assertEqual([row[1] for row in versions], [1, 2])
        self.assertEqual({row[2] for row in versions}, {thumbnail.file_id})
        self.assertEqual(
            [row[3] for row in versions],
            ["Xanh trên thumbnail", "Đỏ trên thumbnail"],
        )
        self.assertEqual(event, (old_id, "CORRECT", "Sửa vùng quan sát"))

        shown: dict[int, str] = {}

        def inspect(dialog: ImageRegionView) -> object:
            shown[current_version] = (
                dialog.region_view.pixmap().toImage().pixelColor(0, 0).name()
            )
            return ImageRegionView.DialogCode.Accepted

        for row in range(2):
            current_version = int(window.evidence_table.item(row, 2).text())
            window.evidence_table.selectRow(row)
            with patch.object(ImageRegionView, "exec", inspect):
                window.reopen_selected_image_evidence()
                self._wait_for_worker(app, window)
        self.assertEqual(
            shown,
            {1: QColor("blue").name(), 2: QColor("red").name()},
        )

        old_row = next(
            row for row in range(2) if window.evidence_table.item(row, 2).text() == "1"
        )
        window.evidence_table.selectRow(old_row)

        def retry_old(dialog: ImageEvidenceDialog) -> object:
            dialog.reason.setText("Thử lại")
            return ImageEvidenceDialog.DialogCode.Accepted

        with (
            patch.object(ImageEvidenceDialog, "exec", retry_old),
            patch("creator_loop.library_ui.QMessageBox.warning"),
        ):
            window.correct_selected_image_evidence()
            self._wait_for_worker(app, window)
        self.assertIn("latest Evidence Version", window.status.text())
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 2
            )

    def test_thumbnail_run_status_is_visible_per_task(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu task"
        root.mkdir()
        db_path = root / "creator_loop.sqlite3"
        initialize(db_path)
        source = Path(self.temp.name) / "Ảnh task.png"
        image = QImage(8, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("green"))
        self.assertTrue(image.save(str(source)))
        imported = intake_image_original(source, root=root)
        stored = root.joinpath(*imported.storage_key.split("/"))
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.table.selectRow(0)
        window.choose_image_thumbnail()
        self._wait_for_worker(app, window)
        self.assertEqual(window.run_table.rowCount(), 1)
        self.assertEqual(window.run_table.item(0, 1).text(), "IMAGE_THUMBNAIL")
        self.assertEqual(window.run_table.item(0, 2).text(), "SUCCEEDED")
        self.assertTrue(
            window.run_table.item(0, 3).text().startswith("storage/derived/")
        )

        changed = bytearray(stored.read_bytes())
        changed[-1] ^= 1
        stored.write_bytes(changed)
        window.table.selectRow(0)
        with patch("creator_loop.library_ui.QMessageBox.warning"):
            window.choose_image_thumbnail()
            self._wait_for_worker(app, window)
        self.assertEqual(window.run_table.rowCount(), 2)
        self.assertEqual(
            {window.run_table.item(row, 2).text() for row in range(2)},
            {"SUCCEEDED", "FAILED"},
        )
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM asset_files WHERE role='THUMBNAIL'"
                ).fetchone()[0],
                1,
            )

    def test_create_and_reopen_video_range_from_ui(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu Video UI"
        root.mkdir()
        initialize(root / "creator_loop.sqlite3")
        fixture = Path(__file__).parent / "fixtures" / "video-red-blue.mp4"
        imported = intake_video_original(fixture, root=root)
        stored = root.joinpath(*imported.storage_key.split("/"))
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.table.selectRow(0)

        def select_range(dialog: VideoEvidenceDialog) -> object:
            dialog.start.setValue(600)
            dialog.end.setValue(800)
            dialog.content.setText("Khung xanh")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            return VideoEvidenceDialog.DialogCode.Accepted

        with patch.object(VideoEvidenceDialog, "exec", select_range):
            window.choose_video_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 1)
        self.assertIn("Video", window.evidence_table.item(0, 0).text())
        window.evidence_table.selectRow(0)
        opened: list[tuple[int, int, str]] = []
        playback_colors: list[str] = []
        playback_cleanup: list[bool] = []

        def inspect_range(dialog: VideoRangeView) -> object:
            opened.append(
                (
                    dialog.segment.start_ms,
                    dialog.segment.end_ms,
                    dialog.segment.player.source().toLocalFile(),
                )
            )

            def capture(frame: object) -> None:
                if frame.isValid():
                    pixel = frame.toImage().pixelColor(0, 0)
                    playback_colors.append(pixel.name())

            dialog.segment.video.videoSink().videoFrameChanged.connect(capture)
            try:
                dialog.segment.play()
                # Match the app's existing response budget, rather than assuming
                # a fresh owned child decodes within two seconds under load.
                deadline = time.monotonic() + 8
                while (
                    not any(
                        QColor(color).blue() > QColor(color).red()
                        for color in playback_colors
                    )
                    and time.monotonic() < deadline
                ):
                    app.processEvents()
                    time.sleep(0.01)
            finally:
                dialog.done(0)
                deadline = time.monotonic() + 12
                while (
                    dialog.segment.player.process is not None
                    and time.monotonic() < deadline
                ):
                    app.processEvents()
                    time.sleep(0.01)
                playback_cleanup.append(
                    dialog.segment.player.process is None
                    and dialog.segment.player.held is None
                )
            return VideoRangeView.DialogCode.Accepted

        with patch.object(VideoRangeView, "exec", inspect_range):
            window.reopen_selected_video_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(opened[0][:2], (600, 800))
        self.assertEqual(Path(opened[0][2]).resolve(), stored.resolve())
        # Assertions in a Qt slot can be printed and swallowed. Check readiness
        # and cleanup on the unittest stack after the callback has returned.
        self.assertTrue(
            any(
                QColor(color).blue() > QColor(color).red() for color in playback_colors
            ),
            playback_colors,
        )
        self.assertEqual(playback_cleanup, [True])

        from creator_loop.claims import EvidenceLink, create_claim

        old_id = window.evidence_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        with closing(sqlite3.connect(root / "creator_loop.sqlite3")) as db:
            db.execute("PRAGMA foreign_keys=ON")
            old_version = db.execute(
                "SELECT * FROM evidence_versions WHERE evidence_version_id=?", (old_id,)
            ).fetchone()
            create_claim(
                db,
                claim_type="FACTUAL",
                statement="Khung hình xanh",
                actor="editor",
                links=[EvidenceLink(str(old_id), "SUPPORTS")],
            )

        def correct_range(dialog: VideoEvidenceDialog) -> object:
            self.assertTrue(dialog.correction)
            self.assertEqual((dialog.start.value(), dialog.end.value()), (600, 800))
            self.assertEqual(dialog.content.text(), "Khung xanh")
            self.assertFalse(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            dialog.start.setValue(100)
            dialog.end.setValue(400)
            dialog.content.setText("Khung đỏ")
            dialog.actor.setText("editor")
            dialog.reason.setText("Chọn lại đoạn")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            dialog.segment.stop()
            return VideoEvidenceDialog.DialogCode.Accepted

        with patch.object(VideoEvidenceDialog, "exec", correct_range):
            window.correct_selected_video_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 2)
        self.assertIn("1 Claim Version", window.status.text())
        with closing(sqlite3.connect(root / "creator_loop.sqlite3")) as db:
            new_id = db.execute(
                "SELECT evidence_version_id FROM evidence_versions WHERE version_no=2"
            ).fetchone()[0]
            self.assertEqual(
                db.execute(
                    "SELECT * FROM evidence_versions WHERE evidence_version_id=?",
                    (old_id,),
                ).fetchone(),
                old_version,
            )
            self.assertEqual(
                db.execute("SELECT evidence_version_id FROM claim_evidence").fetchone()[
                    0
                ],
                old_id,
            )
            self.assertEqual(
                db.execute(
                    "SELECT action,actor_id,reason FROM review_events"
                ).fetchone(),
                ("CORRECT", "editor", "Chọn lại đoạn"),
            )

        def inspect_corrected(dialog: VideoRangeView) -> object:
            self.assertEqual(
                (dialog.segment.start_ms, dialog.segment.end_ms), (100, 400)
            )
            self.assertEqual(
                Path(dialog.segment.player.source().toLocalFile()).resolve(),
                stored.resolve(),
            )
            colors: list[QColor] = []
            dialog.segment.video.videoSink().videoFrameChanged.connect(
                lambda frame: (
                    colors.append(frame.toImage().pixelColor(0, 0))
                    if frame.isValid()
                    else None
                )
            )
            dialog.segment.play()
            deadline = time.monotonic() + 2
            while (
                not any(color.red() > color.blue() for color in colors)
                and time.monotonic() < deadline
            ):
                app.processEvents()
                time.sleep(0.01)
            self.assertTrue(any(color.red() > color.blue() for color in colors))
            dialog.done(0)
            return VideoRangeView.DialogCode.Accepted

        for row in range(window.evidence_table.rowCount()):
            if (
                window.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                == new_id
            ):
                window.evidence_table.selectRow(row)
        with patch.object(VideoRangeView, "exec", inspect_corrected):
            window.reopen_selected_video_evidence()
            self._wait_for_worker(app, window)
        for row in range(window.evidence_table.rowCount()):
            if (
                window.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                == old_id
            ):
                window.evidence_table.selectRow(row)
        with patch.object(VideoRangeView, "exec", inspect_range):
            window.reopen_selected_video_evidence()
            self._wait_for_worker(app, window)
        with (
            patch.object(VideoEvidenceDialog, "exec", correct_range),
            patch("creator_loop.library_ui.QMessageBox.warning") as stale,
        ):
            window.correct_selected_video_evidence()
            self._wait_for_worker(app, window)
        stale.assert_called_once()
        self.assertIn("latest Evidence Version", window.status.text())
        self.assertEqual(window.evidence_table.rowCount(), 2)

    def test_create_and_reopen_audio_range_from_ui(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "dữ liệu Audio UI"
        root.mkdir()
        initialize(root / "creator_loop.sqlite3")
        fixture = Path(__file__).parent / "fixtures" / "video-with-tone.mp4"
        imported = intake_video_original(fixture, root=root)
        stored = root.joinpath(*imported.storage_key.split("/"))
        window = LibraryWindow(root)
        self.addCleanup(window.close)
        window.show()
        window.table.selectRow(0)

        def select_range(dialog: AudioEvidenceDialog) -> object:
            dialog.start.setValue(200)
            dialog.end.setValue(600)
            dialog.kind.setCurrentIndex(1)
            dialog.content.setText("Âm tổng hợp 440 Hz")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            dialog.segment.stop()
            return AudioEvidenceDialog.DialogCode.Accepted

        with patch.object(AudioEvidenceDialog, "exec", select_range):
            window.choose_audio_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 1)
        self.assertIn("Audio", window.evidence_table.item(0, 0).text())
        window.evidence_table.selectRow(0)
        with patch("creator_loop.library_ui.QMessageBox.information") as wrong_track:
            window.reopen_selected_video_evidence()
        wrong_track.assert_called_once()
        opened: list[tuple[int, int, str]] = []

        def inspect_range(dialog: AudioRangeView) -> object:
            opened.append(
                (
                    dialog.segment.start_ms,
                    dialog.segment.end_ms,
                    dialog.segment.player.source().toLocalFile(),
                )
            )
            dialog.segment.stop()
            return AudioRangeView.DialogCode.Accepted

        with patch.object(AudioRangeView, "exec", inspect_range):
            window.reopen_selected_audio_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(opened[0][:2], (200, 600))
        self.assertEqual(Path(opened[0][2]).resolve(), stored.resolve())

        old_id = window.evidence_table.item(0, 0).data(Qt.ItemDataRole.UserRole)

        def correct_range(dialog: AudioEvidenceDialog) -> object:
            self.assertTrue(dialog.correction)
            self.assertEqual((dialog.start.value(), dialog.end.value()), (200, 600))
            self.assertEqual(dialog.content.text(), "Âm tổng hợp 440 Hz")
            self.assertEqual(dialog.kind.currentData(), "OTHER")
            self.assertFalse(dialog.kind.isEnabled())
            dialog.start.setValue(600)
            dialog.end.setValue(900)
            dialog.content.setText("Đoạn âm đã sửa")
            dialog.actor.setText("editor")
            dialog.reason.setText("Chọn lại audio")
            self.assertTrue(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            dialog.segment.stop()
            return AudioEvidenceDialog.DialogCode.Accepted

        with patch.object(AudioEvidenceDialog, "exec", correct_range):
            window.correct_selected_audio_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(window.evidence_table.rowCount(), 2)
        with closing(sqlite3.connect(root / "creator_loop.sqlite3")) as db:
            new_id = db.execute(
                "SELECT evidence_version_id FROM evidence_versions WHERE version_no=2"
            ).fetchone()[0]
            self.assertEqual(
                db.execute("SELECT evidence_type FROM evidences").fetchone()[0], "OTHER"
            )
            self.assertEqual(
                db.execute(
                    "SELECT action,actor_id,reason FROM review_events"
                ).fetchone(),
                ("CORRECT", "editor", "Chọn lại audio"),
            )
            self.assertEqual(
                db.execute(
                    "SELECT content FROM evidence_versions WHERE evidence_version_id=?",
                    (old_id,),
                ).fetchone()[0],
                "Âm tổng hợp 440 Hz",
            )

        def inspect_corrected(dialog: AudioRangeView) -> object:
            self.assertEqual(
                (dialog.segment.start_ms, dialog.segment.end_ms), (600, 900)
            )
            self.assertEqual(
                Path(dialog.segment.player.source().toLocalFile()).resolve(),
                stored.resolve(),
            )
            dialog.segment.stop()
            return AudioRangeView.DialogCode.Accepted

        for row in range(window.evidence_table.rowCount()):
            if (
                window.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                == new_id
            ):
                window.evidence_table.selectRow(row)
        with patch.object(AudioRangeView, "exec", inspect_corrected):
            window.reopen_selected_audio_evidence()
            self._wait_for_worker(app, window)
        for row in range(window.evidence_table.rowCount()):
            if (
                window.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                == old_id
            ):
                window.evidence_table.selectRow(row)
        with patch.object(AudioRangeView, "exec", inspect_range):
            window.reopen_selected_audio_evidence()
            self._wait_for_worker(app, window)
        with (
            patch.object(AudioEvidenceDialog, "exec", correct_range),
            patch("creator_loop.library_ui.QMessageBox.warning") as stale,
        ):
            window.correct_selected_audio_evidence()
            self._wait_for_worker(app, window)
        stale.assert_called_once()
        self.assertIn("latest Evidence Version", window.status.text())

        from creator_loop.evidence import Evidence, EvidenceRepository, EvidenceVersion

        with closing(sqlite3.connect(root / "creator_loop.sqlite3")) as db:
            db.execute("PRAGMA foreign_keys=ON")
            EvidenceRepository(db).create_with_version(
                Evidence(
                    "unsupported-audio",
                    imported.asset_id,
                    "METADATA",
                    "2026-10-02T00:00:00Z",
                    None,
                ),
                EvidenceVersion(
                    "unsupported-audio-v1",
                    "unsupported-audio",
                    imported.asset_id,
                    1,
                    imported.file_id,
                    "Unsupported kind",
                    "TIME_RANGE",
                    '{"start_ms":200,"end_ms":600,"track":"audio"}',
                    "HUMAN",
                    None,
                    "creator",
                    "2026-10-02T00:00:00Z",
                ),
            )
        window.reload()
        for row in range(window.evidence_table.rowCount()):
            if (
                window.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                == "unsupported-audio-v1"
            ):
                window.evidence_table.selectRow(row)
        with (
            patch.object(AudioEvidenceDialog, "exec") as unsupported_dialog,
            patch("creator_loop.library_ui.QMessageBox.warning") as unsupported,
        ):
            window.correct_selected_audio_evidence()
            self._wait_for_worker(app, window)
        unsupported.assert_called_once()
        unsupported_dialog.assert_not_called()
        self.assertIn("SPEECH or OTHER", window.status.text())
        with closing(sqlite3.connect(root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0], 3
            )
