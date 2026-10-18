"""Pure planning, timeline, subtitle and metadata boundaries."""

import json
import tempfile
import unittest
from pathlib import Path

from creator_loop.backend.analysis.content_analysis import draft_body
from creator_loop.backend.video.planner import plan_draft
from creator_loop.backend.video.subtitles import subtitle_filters, write_srt
from creator_loop.backend.video.timeline import (
    VideoPlanError,
    build_timeline,
    subtitles_from_timeline,
)
from creator_loop.backend.video.tools import LocalToolError, parse_probe
from creator_loop.shared.workflow import (
    DraftSnapshot,
    RenderPreset,
    Scene,
    Subtitle,
    VerifiedMedia,
)


class PlanningTests(unittest.TestCase):
    def test_actual_creator_draft_renders_declared_plan_without_metadata_or_claims(
        self,
    ):
        body = draft_body(
            direction="Hướng được review",
            purpose="nhận thức đúng về sự thật",
            audience="người đọc",
            style="rõ ràng",
            output_format="VIDEO",
            citations=("exact-claim-v1", "exact-claim-v2"),
        )
        draft = DraftSnapshot(
            "creator-video-v1",
            "project",
            1,
            body,
            "VIDEO",
            ("exact-claim-v1", "exact-claim-v2"),
        )
        scenes = plan_draft(draft)
        self.assertEqual(len(scenes), 4)
        self.assertEqual([scene.duration_ms for scene in scenes], [4000] * 4)
        self.assertTrue(
            all(scene.kind == "TITLE" and scene.file_id is None for scene in scenes)
        )
        self.assertEqual(scenes[0].text, "Điều ta tin / điều đã kiểm chứng")
        self.assertEqual(build_timeline(scenes, RenderPreset()).duration_ms, 16000)
        rendered_text = "\n".join(scene.text for scene in scenes)
        self.assertNotIn("Mục đích", rendered_text)
        self.assertNotIn("exact-claim", rendered_text)
        self.assertNotIn("LỜI ĐỌC", rendered_text)
        self.assertEqual(draft.body_text, body)
        self.assertIn("exact-claim-v1", draft.body_text)

    def test_declared_scene_plan_is_strict_and_never_falls_back_to_partial_script(self):
        body = draft_body(
            direction="x",
            purpose="x",
            audience="x",
            style="x",
            output_format="VIDEO",
            citations=(),
        )
        for modified in (
            body.replace("LỜI ĐỌC", "LỜI ĐỌC BỊ THIẾU"),
            body.replace("2. Chữ:", "3. Chữ:"),
            body.replace("— 4 giây", "— không rõ giây", 1),
            body.replace("1. Tiêu đề:", "1. Video:"),
        ):
            with self.subTest(modified=modified), self.assertRaises(VideoPlanError):
                plan_draft(DraftSnapshot("d", "p", 1, modified, "VIDEO"))

    def test_plan_keeps_script_and_does_not_auto_use_unapproved_images_or_guess_video(
        self,
    ):
        draft = DraftSnapshot(
            "d1",
            "p",
            1,
            "Hook: Hãy hỏi căn cứ.\nLời đọc: Quan sát khác với suy luận.",
            "VIDEO",
        )
        material = VerifiedMedia(
            "file", "asset", Path("ignored.png"), "0" * 64, "IMAGE", False
        )
        scenes = plan_draft(draft, (material,))
        self.assertEqual(
            [scene.text for scene in scenes],
            ["Hãy hỏi căn cứ.", "Quan sát khác với suy luận."],
        )
        self.assertTrue(
            all(scene.kind == "TITLE" and scene.file_id is None for scene in scenes)
        )
        approved = VerifiedMedia(
            "file", "asset", Path("ignored.png"), "0" * 64, "IMAGE", True
        )
        self.assertEqual(plan_draft(draft, (approved,))[0].file_id, "file")

    def test_large_script_reports_budget_instead_of_silently_truncating(self):
        script = " ".join(["sự thật cần kiểm chứng"] * 90)
        with self.assertRaises(VideoPlanError):
            plan_draft(DraftSnapshot("d", "p", 1, script, "VIDEO"))

    def test_timeline_aligns_frames_and_rejects_missing_file_or_out_of_range_subtitles(
        self,
    ):
        timeline = build_timeline(
            (Scene("Chữ", 1100), Scene("Chữ khác", 1500)), RenderPreset()
        )
        self.assertEqual(timeline.frames, 63)
        self.assertEqual(timeline.duration_ms, 2625)
        self.assertEqual(subtitles_from_timeline(timeline)[1].start_ms, 1125)
        with self.assertRaises(VideoPlanError):
            build_timeline((Scene("", 3000, "VIDEO", "url-only"),), RenderPreset())
        with self.assertRaises(VideoPlanError):
            build_timeline(
                (Scene("Chữ", 3000),),
                RenderPreset(),
                subtitles=(Subtitle(0, 5000, "ngoài timeline"),),
            )

    def test_subtitle_text_is_file_content_and_numeric_times_not_filter_syntax(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            text = "Sự thật %{expr:bad}: {x} \\ và tiếng Việt"
            subtitles = (Subtitle(100, 1500, text),)
            filters = subtitle_filters(workspace, subtitles, RenderPreset())
            self.assertNotIn(text, filters[0])
            self.assertIn("expansion=none", filters[0])
            self.assertIn("gte(t,0.100)*lt(t,1.500)", filters[0])
            self.assertIn(
                "%{expr:bad}",
                (workspace / "subtitle-000.txt").read_text(encoding="utf-8"),
            )
            write_srt(workspace / "captions.srt", subtitles)
            self.assertIn(
                "00:00:00,100 --> 00:00:01,500",
                (workspace / "captions.srt").read_text(encoding="utf-8"),
            )

    def test_probe_preserves_unknown_values_and_rejects_malformed_metadata(self):
        known = parse_probe(
            json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "png",
                            "width": 320,
                            "height": 240,
                        }
                    ],
                    "format": {"size": "120", "format_name": "png_pipe"},
                }
            )
        )
        self.assertIsNone(known.duration_ms)
        self.assertIsNone(known.audio_codec)
        self.assertEqual(known.width, 320)
        for raw in (
            "{}",
            '{"streams":[null]}',
            '{"streams":[],"format":{"duration":"NaN"}}',
        ):
            with self.subTest(raw=raw), self.assertRaises(LocalToolError):
                parse_probe(raw)


if __name__ == "__main__":
    unittest.main()
