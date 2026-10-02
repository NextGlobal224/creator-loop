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
        from creator_loop.image_evidence_ui import ImageEvidenceDialog, ImageRegionView
        from creator_loop.library_ui import LibraryWindow
        from creator_loop.media_intake import (
            intake_image_original,
            intake_video_original,
        )
        from creator_loop.source_ui import SourceDialog
        from creator_loop.text_evidence_ui import TextEvidenceDialog
        from creator_loop.text_intake import intake_text_original
        from creator_loop.video_evidence_ui import VideoEvidenceDialog, VideoRangeView
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog
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
        samples = (
            ("TEXT", "Bản ghi Đà Nẵng.txt", "Một đoạn văn".encode("utf-8")),
            ("VIDEO", "Huế Kha.mp4", b"\x00\x00\x00\x18ftypisom" + b"sample"),
            ("IMAGE", "Ảnh mẫu.png", b"\x89PNG\r\n\x1a\n" + b"sample"),
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

    def test_text_range_dialog_uses_unicode_code_points(self) -> None:
        assert QApplication is not None
        app = QApplication.instance() or QApplication([])
        dialog = TextEvidenceDialog("😀Café")
        self.addCleanup(dialog.close)
        dialog.start.setValue(1)
        dialog.end.setValue(5)
        app.processEvents()
        self.assertEqual(dialog.preview.text(), "Café")

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

        def inspect_range(dialog: VideoRangeView) -> object:
            opened.append(
                (
                    dialog.segment.start_ms,
                    dialog.segment.end_ms,
                    dialog.segment.player.source().toLocalFile(),
                )
            )
            colors: list[str] = []

            def capture(frame: object) -> None:
                if frame.isValid():
                    pixel = frame.toImage().pixelColor(0, 0)
                    colors.append(pixel.name())

            dialog.segment.video.videoSink().videoFrameChanged.connect(capture)
            dialog.segment.play()
            deadline = time.monotonic() + 2
            while (
                not any(QColor(color).blue() > QColor(color).red() for color in colors)
                and time.monotonic() < deadline
            ):
                app.processEvents()
                time.sleep(0.01)
            self.assertTrue(
                any(QColor(color).blue() > QColor(color).red() for color in colors)
            )
            dialog.done(0)
            return VideoRangeView.DialogCode.Accepted

        with patch.object(VideoRangeView, "exec", inspect_range):
            window.reopen_selected_video_evidence()
            self._wait_for_worker(app, window)
        self.assertEqual(opened[0][:2], (600, 800))
        self.assertEqual(Path(opened[0][2]).resolve(), stored.resolve())
