import unittest

from creator_loop.backend.analysis.content_analysis import analyze_material, draft_body
from creator_loop.shared.workflow import ResearchMaterial


class ContentAnalysisTests(unittest.TestCase):
    def test_caption_scope_does_not_claim_whole_video_or_cause_of_engagement(self):
        material = ResearchMaterial(
            "s",
            "source",
            "asset",
            "file",
            "0" * 64,
            "TEXT",
            "Bạn đã kiểm chứng chưa?\n1. Có 30 người chia sẻ.\nHãy kiểm tra nguồn.",
            evidence_version_ids=("ev1",),
            scope="CAPTION_ONLY",
        )
        result = analyze_material(material)
        self.assertEqual(result.evidence_version_ids, ("ev1",))
        self.assertIn("chưa xử lý toàn ảnh/video", result.limitation)
        self.assertIn("có liệt kê", result.structure)
        self.assertEqual(result.data_cues, ("1. Có 30 người chia sẻ.",))
        self.assertIn(
            "chưa có text",
            analyze_material(
                ResearchMaterial(
                    "url", "s", None, None, None, "REFERENCE", "", scope="URL_ONLY"
                )
            ).hook,
        )

    def test_local_editorial_formats_cite_exact_claims_without_copying_hook(self):
        for output_format in ("TEXT", "IMAGE_CAPTION", "VIDEO"):
            text = draft_body(
                direction="Chọn kiểm chứng",
                purpose="nhận thức đúng về sự thật",
                audience="Người đọc",
                style="thận trọng",
                output_format=output_format,
                citations=("claim-exact-v3",),
            )
            self.assertIn("claim-exact-v3", text)
            self.assertIn("chờ Creator review", text)
            self.assertNotIn("chắc chắn viral", text)
        self.assertIn("KẾ HOẠCH CẢNH", text)
