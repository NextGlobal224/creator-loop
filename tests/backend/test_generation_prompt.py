import unittest

from creator_loop.backend.analysis.generation_prompt import generation_prompt
from creator_loop.shared.workflow import CreatorRequest, ResearchMaterial


class GenerationPromptTests(unittest.TestCase):
    def test_different_project_topic_and_video_request_are_not_forced_to_article(self):
        request = CreatorRequest(
            "p",
            "MANUAL",
            "Tự học ngoại ngữ",
            "Người mới học",
            "Gần gũi",
            "VIDEO",
            ("m",),
            "Luyện nghe 15 phút",
        )
        material = ResearchMaterial(
            "m", None, None, None, None, "TEXT", "Một tư liệu phương pháp học."
        )
        prompt = generation_prompt(request, [material])
        self.assertIn("KẾ HOẠCH CẢNH", prompt)
        self.assertIn("Luyện nghe 15 phút", prompt)
        self.assertIn("Tự học ngoại ngữ", prompt)
        self.assertNotIn("ảnh chụp màn hình", prompt)
        self.assertNotIn("Bài hoàn chỉnh 250", prompt)

    def test_video_contract_preserves_narration_and_only_plans_scene_section(self):
        from creator_loop.backend.video.planner import plan_draft
        from creator_loop.shared.workflow import DraftSnapshot, RenderPreset

        body = (
            "KỊCH BẢN VIDEO NHÁP\nMỞ BÀI\nMột tình huống giả định.\nTÀI SẢN\nCần chọn qua C.\nLỜI ĐỌC\nLời đọc được giữ riêng.\nKẾ HOẠCH CẢNH\n"
            + "\n".join(f"{i}. Chữ: Cảnh {i} — 5 giây" for i in range(1, 7))
        )
        draft = DraftSnapshot("d", "p", 1, body, "VIDEO")
        scenes = plan_draft(draft, (), RenderPreset())
        self.assertEqual([s.text for s in scenes], [f"Cảnh {i}" for i in range(1, 7)])
        self.assertEqual(sum(s.duration_ms for s in scenes), 30000)
        self.assertIn("Lời đọc được giữ riêng.", draft.body_text)
