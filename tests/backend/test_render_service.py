import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creator_loop.backend.creator.drafts import append_draft_version, create_draft
from creator_loop.backend.runtime.database import _connect_write, initialize
from creator_loop.backend.runtime.paths import ensure_data_root
from creator_loop.backend.video.render_service import RenderService
from creator_loop.shared.workflow import RenderPreset, RenderResult, Scene


@unittest.skipUnless(
    sys.platform == "win32", "Original intake uses owned Windows file handles"
)
class RenderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu test có khoảng trắng"
        ensure_data_root(self.root)
        initialize(self.root / "creator_loop.sqlite3")
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            db.execute("INSERT INTO projects VALUES('p','Truth','ACTIVE','now',NULL)")
            db.commit()
            self.draft = create_draft(
                db,
                project_id="p",
                body_text="Kiểm chứng trước khi tin.\nĐọc nguồn và căn cứ.",
                format="VIDEO",
                actor="creator",
            )
        self.service = RenderService(self.root)
        specs = tuple(
            SimpleNamespace(component_id=key, version="test", sha256="e" * 64)
            for key in ("ffmpeg-exe", "ffprobe-exe")
        )
        self.selection = patch(
            "creator_loop.backend.video.render_service.load_video_components",
            return_value=specs,
        )
        self.selection.start()
        self.addCleanup(self.selection.stop)
        self.identity = patch(
            "creator_loop.backend.video.render_service.renderer_identity",
            return_value={
                "implementation_version": "test",
                "font_path": "fixture-font.ttf",
                "configuration": {"font_sha256": "f" * 64},
            },
        )
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def prepare(self):
        return self.service.prepare(
            self.draft.draft_version_id,
            (Scene("Sự thật cần căn cứ", 2000),),
            RenderPreset(),
        )

    def test_cancel_has_no_output_keeps_exact_draft_and_script(self):
        request = self.prepare()
        self.service.finish(
            RenderResult(request.run_id, request.draft.draft_version_id, "CANCELLED")
        )
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM draft_render_outputs").fetchone()[0], 0
            )
            row = db.execute(
                "SELECT r.status,f.parent_file_id,f.asset_id=r.asset_id FROM processing_runs r JOIN asset_files f ON f.file_id=r.input_file_id WHERE r.run_id=?",
                (request.run_id,),
            ).fetchone()
            self.assertEqual(row, ("CANCELLED", None, 1))
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )
            payload = json.loads(
                db.execute("SELECT request_json FROM draft_render_requests").fetchone()[
                    0
                ]
            )
            self.assertEqual(payload["draft_version_id"], self.draft.draft_version_id)

    def test_changed_output_is_rejected_and_cannot_be_previewed(self):
        request = self.prepare()
        path = request.output_directory / "bad.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42bad")
        result = RenderResult(
            request.run_id,
            request.draft.draft_version_id,
            "SUCCEEDED",
            path,
            "b" * 64,
            path.stat().st_size,
            2000,
            360,
            640,
            "ffmpeg",
            "test",
        )
        with self.assertRaises(ValueError):
            self.service.finish(result)
        with self.assertRaises(ValueError):
            self.service.completed_output(request.run_id)

    def test_new_draft_render_preserves_old_output_and_inputs(self):
        request = self.prepare()
        # A fake container is used only for the persistence guard. Actual MP4 QA
        # is a separate FFmpeg/ffprobe test, never inferred from this fixture.
        path = request.output_directory / "fixture.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.service.finish(
            RenderResult(
                request.run_id,
                request.draft.draft_version_id,
                "SUCCEEDED",
                path,
                digest,
                path.stat().st_size,
                2000,
                360,
                640,
                "ffmpeg",
                "test",
            )
        )
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            with patch(
                "creator_loop.backend.video.renderer.LocalFFmpegRenderer",
                side_effect=AssertionError("Cached inputs must not render again"),
            ):
                cached = self.service.render(
                    request.draft.draft_version_id,
                    request.scenes,
                    request.preset,
                    lambda _progress: None,
                    lambda: False,
                )
            self.assertEqual(cached.run_id, request.run_id)
            with patch(
                "creator_loop.backend.video.render_service.renderer_identity",
                return_value={
                    "implementation_version": "changed",
                    "font_path": "fixture-font.ttf",
                    "configuration": {"font_sha256": "e" * 64},
                },
            ):
                self.assertIsNone(
                    self.service.cached_result(
                        request.draft.draft_version_id,
                        request.scenes,
                        request.preset,
                        (),
                        None,
                    )
                )
            new = append_draft_version(
                db,
                draft_version_id=self.draft.draft_version_id,
                body_text="Chỉnh lời đọc và kiểm nguồn.",
                format="VIDEO",
                actor="creator",
            )
        next_request = self.service.prepare(
            new.draft_version_id, (Scene("Lời đọc mới", 1000),), RenderPreset()
        )
        self.assertNotEqual(request.run_id, next_request.run_id)
        self.assertEqual(self.service.completed_output(request.run_id), path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(
            self.service.result(request.run_id).draft_version_id,
            self.draft.draft_version_id,
        )

    def test_unknown_reuse_media_cannot_become_a_scene(self):
        with self.assertRaises(ValueError):
            self.service.prepare(
                self.draft.draft_version_id,
                (Scene("Ảnh", 1000, "IMAGE", "unknown-file"),),
                RenderPreset(),
            )


if __name__ == "__main__":
    unittest.main()
