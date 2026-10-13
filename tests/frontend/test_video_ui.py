import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.creator.drafts import create_draft
from creator_loop.backend.runtime.database import _connect_write, initialize
from creator_loop.backend.runtime.paths import ensure_data_root
from creator_loop.frontend.video_ui import VideoDialog
from creator_loop.shared.workflow import RenderResult
from PySide6.QtWidgets import QApplication


class VideoUiTests(unittest.TestCase):
    def test_real_editor_dispatch_and_cancel_remain_responsive_and_history_safe(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ensure_data_root(root)
            initialize(root / "creator_loop.sqlite3")
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                db.execute(
                    "INSERT INTO projects VALUES('p','Truth','ACTIVE','now',NULL)"
                )
                db.commit()
                draft = create_draft(
                    db,
                    project_id="p",
                    body_text="Kiểm chứng nguồn.\nĐọc căn cứ.",
                    format="VIDEO",
                    actor="creator",
                )
            dialog = VideoDialog(root, draft_version_id=draft.draft_version_id)
            self.assertEqual(dialog.scenes.rowCount(), 2)

            def render(_version, _scenes, _preset, _progress, cancelled, *_args):
                deadline = time.monotonic() + 3
                while not cancelled() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return RenderResult(
                    "fake-owned-run", draft.draft_version_id, "CANCELLED"
                )

            try:
                with patch.object(dialog.service, "render", side_effect=render):
                    dialog.render()
                    self.assertFalse(dialog.render_button.isEnabled())
                    self.assertTrue(dialog.cancel_button.isEnabled())
                    app.processEvents()
                    dialog.cancel()
                    deadline = time.monotonic() + 5
                    while dialog._worker is not None and time.monotonic() < deadline:
                        app.processEvents()
                        time.sleep(0.01)
                    self.assertIsNone(dialog._worker)
                    self.assertIn("Đã Hủy", dialog.status.text())
                    self.assertEqual(dialog.history.rowCount(), 0)
                    self.assertTrue(dialog.render_button.isEnabled())
            finally:
                dialog.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
