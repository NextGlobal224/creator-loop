from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from creator_loop.backend.creator.projects import create_project
from creator_loop.backend.creator.research_pipeline import ResearchPipeline
from creator_loop.backend.knowledge.evidence import EvidenceRepository, EvidenceVersion
from creator_loop.backend.knowledge.evidence_reopen import reopen_evidence_version
from creator_loop.backend.runtime.database import (
    _connect_write,
    initialize,
    open_readonly,
)
from creator_loop.shared.workflow import CreatorRequest


def fixture(root: Path) -> CreatorRequest:
    initialize(root / "creator_loop.sqlite3")
    text = "Facebook capture\n\nNiềm tin cần được kiểm chứng bằng sự thật. Hãy tách điều quan sát khỏi suy diễn."
    file = root / "storage/originals/f.txt"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(text.encode("utf-8"))
    with closing(_connect_write(root / "creator_loop.sqlite3")) as db, db:
        project = create_project(db, title="Kiểm chứng sự thật")
        db.execute("INSERT INTO assets VALUES('a','TEXT','Caption','now',NULL)")
        db.execute(
            "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES('f','a','ORIGINAL','storage/originals/f.txt',?,?,'text/plain','now')",
            (hashlib.sha256(file.read_bytes()).hexdigest(), file.stat().st_size),
        )
        db.execute(
            "INSERT INTO sources VALUES('s','FB','https://www.facebook.com/user/posts/12','12','Fixture',NULL,'now','REFERENCE_ONLY','now')"
        )
        db.execute(
            "INSERT INTO source_assets VALUES('s','a','REFERENCE','UNREVIEWED','now')"
        )
        db.execute(
            "INSERT INTO source_captures VALUES('s','sự thật','now',?,'f')",
            (text.split("\n\n")[1],),
        )
        db.execute(
            "INSERT INTO sources VALUES('url','FB','https://www.facebook.com/reel/13','13','Fixture',NULL,'now','UNKNOWN','now')"
        )
    return CreatorRequest(
        project.project_id,
        "MANUAL",
        "nhận thức đúng về sự thật",
        "người đọc",
        "thận trọng",
        "VIDEO",
        ("source:s",),
    )


class ResearchPipelineTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Native owned Original intake")
    def test_image_attach_preserves_exact_caption_and_blocks_changed_bytes_on_reopen(
        self,
    ):
        from creator_loop.backend.library.media_intake import intake_image_original

        image = Path(__file__).resolve().parents[2] / "docs/examples/B/image.png"
        imported = intake_image_original(image, root=self.root)
        wid = self.pipeline.analyze(
            replace(self.request, output_format="IMAGE_CAPTION")
        )
        first = self.pipeline.create_draft(
            wid, body_text="Caption đã lưu; không sinh lại."
        )
        second = self.pipeline.create_draft(
            wid,
            image_file_id=imported.file_id,
            image_provenance="Original release illustration",
            image_usage_note="CC0-1.0",
        )
        self.assertEqual(second.body_text, first.body_text)
        self.assertEqual(second.claim_version_ids, first.claim_version_ids)
        self.assertEqual(second.version_no, first.version_no + 1)
        self.assertEqual(
            ResearchPipeline(self.root).draft(second.draft_version_id), second
        )
        self.assertEqual(
            self.pipeline.create_draft(wid).draft_version_id, second.draft_version_id
        )
        (self.root / imported.storage_key).write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.pipeline.draft(second.draft_version_id)
        # UI metadata cannot claim byte verification and must not hash on GUI thread.
        self.assertEqual(
            self.pipeline.draft_metadata(second.draft_version_id).image_file_id,
            imported.file_id,
        )

    def test_manual_direction_is_saved_per_version_and_restored(self):
        wid = self.pipeline.analyze(self.request)
        first = self.pipeline.create_draft(
            wid,
            "Hướng được người viết điều chỉnh",
            body_text="Nội dung thủ công cần review.",
        )
        reopened = ResearchPipeline(self.root).draft(first.draft_version_id)
        self.assertEqual(reopened.direction, "Hướng được người viết điều chỉnh")
        self.assertEqual(
            self.pipeline.create_draft(wid).draft_version_id, first.draft_version_id
        )
        second = self.pipeline.create_draft(
            wid, "Hướng điều chỉnh lần hai", body_text=first.body_text
        )
        self.assertEqual(second.version_no, first.version_no + 1)
        self.assertEqual(
            self.pipeline.draft(first.draft_version_id).direction, reopened.direction
        )
        self.assertEqual(second.body_text, first.body_text)

    def test_manual_exact_claim_assertions_and_same_body_citation_edit_are_versioned(
        self,
    ):
        from creator_loop.backend.creator.drafts import DraftAssertion, DraftClaimLink

        pipeline = ResearchPipeline(self.root)
        wid = pipeline.analyze(self.request)
        cid = pipeline.load(wid)["claims"][0]
        body = "Đây là nhận định cần người dùng kiểm tra."
        first = pipeline.create_draft(wid, body_text=body)
        second = pipeline.create_draft(
            wid,
            body_text=body,
            claims=[DraftClaimLink(cid, "ASSERTED")],
            assertions=[DraftAssertion(0, len(body), cid, "UNREVIEWED")],
        )
        self.assertEqual(second.version_no, first.version_no + 1)
        self.assertNotEqual(first.draft_version_id, second.draft_version_id)
        self.assertEqual(
            pipeline.create_draft(wid).draft_version_id, second.draft_version_id
        )
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT claim_version_id,review_state FROM draft_assertions WHERE draft_version_id=?",
                    (second.draft_version_id,),
                ).fetchone(),
                (cid, "UNREVIEWED"),
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM draft_render_requests").fetchone()[0],
                0,
            )

    def test_editing_manual_body_keeps_exact_claims_and_marks_content_needs_source(
        self,
    ):
        pipeline = ResearchPipeline(self.root)
        wid = pipeline.analyze(self.request)
        first = pipeline.create_draft(
            wid, body_text="Nội dung thủ công có căn cứ cần kiểm tra."
        )
        changed = pipeline.create_draft(
            wid, body_text="Nội dung mới cần đối chiếu lại nguồn."
        )
        self.assertEqual(first.claim_version_ids, changed.claim_version_ids)
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT review_state FROM draft_assertions WHERE draft_version_id=?",
                    (changed.draft_version_id,),
                ).fetchone()[0],
                "NEEDS_SOURCE",
            )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = fixture(self.root)
        self.pipeline = ResearchPipeline(self.root)

    def counts(self):
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            return tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "creator_workflows",
                    "processing_runs",
                    "evidence_versions",
                    "claim_versions",
                    "draft_versions",
                    "review_events",
                    "selection_events",
                    "approvals",
                )
            )

    def test_grounded_pipeline_cache_switch_mode_reopen_and_exact_versions(self):
        wid = self.pipeline.analyze(self.request)
        value = self.pipeline.load(wid)
        self.assertIn("CAPTION_ONLY", value["analysis_text"])
        self.assertEqual(len(value["claims"]), 1)
        snapshot = self.pipeline.create_draft(wid)
        self.assertEqual(snapshot.format, "VIDEO")
        self.assertEqual(snapshot.claim_version_ids, tuple(value["claims"]))
        self.assertEqual(self.counts(), (1, 1, 1, 1, 1, 0, 0, 0))
        reopened = ResearchPipeline(self.root)
        with self.assertRaisesRegex(ValueError, "không thay bằng template"):
            reopened.analyze(replace(self.request, mode="AUTOMATIC"))
        self.assertEqual(reopened.analyze(self.request), wid)
        self.assertEqual(reopened.create_draft(wid), snapshot)
        self.assertEqual(reopened.draft(snapshot.draft_version_id), snapshot)
        self.assertEqual(self.counts(), (1, 1, 1, 1, 1, 0, 0, 0))
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            ev = db.execute(
                "SELECT evidence_version_id,producer_type,processing_run_id FROM evidence_versions"
            ).fetchone()
            self.assertEqual(ev[1], "TOOL")
            self.assertIsNotNone(ev[2])
            anchor = reopen_evidence_version(db, ev[0], self.root)
            self.assertNotIn("Facebook capture", anchor.text_excerpt)
            self.assertEqual(
                db.execute("SELECT claim_type FROM claims").fetchone()[0],
                "EDITORIAL_HYPOTHESIS",
            )

    def test_direction_or_manual_body_edit_appends_version_keeps_old(self):
        wid = self.pipeline.analyze(self.request)
        first = self.pipeline.create_draft(wid)
        second = self.pipeline.create_draft(wid, "Tập trung vào nguồn tin")
        third = self.pipeline.create_draft(
            wid, body_text="Bản nháp do Creator viết lại; cần review."
        )
        self.assertEqual(
            (first.version_no, second.version_no, third.version_no), (1, 2, 3)
        )
        self.assertEqual(self.pipeline.draft(first.draft_version_id), first)
        self.assertEqual(first.claim_version_ids, third.claim_version_ids)
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM draft_version_parents").fetchone()[0],
                2,
            )
            self.assertEqual(
                db.execute(
                    "SELECT created_by FROM draft_versions WHERE draft_version_id=?",
                    (third.draft_version_id,),
                ).fetchone()[0],
                "creator",
            )

    def test_url_only_and_unprocessed_video_cannot_create_grounded_draft(self):
        wid = self.pipeline.analyze(replace(self.request, material_ids=("source:url",)))
        self.assertIn("URL_ONLY", self.pipeline.load(wid)["analysis_text"])
        with self.assertRaisesRegex(ValueError, "URL-only"):
            self.pipeline.create_draft(wid)
        self.assertEqual(self.counts(), (1, 0, 0, 0, 0, 0, 0, 0))

    def test_digest_mismatch_blocks_even_cached_analysis_and_draft(self):
        wid = self.pipeline.analyze(self.request)
        before = self.counts()
        (self.root / "storage/originals/f.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.pipeline.analyze(self.request)
        with self.assertRaises(ValueError):
            self.pipeline.create_draft(wid)
        self.assertEqual(self.counts(), before)

    def test_human_evidence_new_version_invalidates_analysis_cache_without_rewriting(
        self,
    ):
        wid = self.pipeline.analyze(self.request)
        value = self.pipeline.load(wid)
        ev = value["materials"][0]["evidence_version_id"]
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            row = db.execute(
                "SELECT evidence_id,asset_id,anchor_file_id,content,locator_data FROM evidence_versions WHERE evidence_version_id=?",
                (ev,),
            ).fetchone()
            locator = json.loads(row[4])
            version = EvidenceVersion(
                "corrected",
                row[0],
                row[1],
                2,
                row[2],
                row[3],
                "TEXT_RANGE",
                json.dumps(locator, sort_keys=True),
                "HUMAN",
                None,
                "creator",
                "now",
            )
            EvidenceRepository(db).append_version(version)
        new = self.pipeline.analyze(self.request)
        self.assertNotEqual(new, wid)
        self.assertEqual(
            self.pipeline.load(new)["materials"][0]["evidence_version_id"], "corrected"
        )
        self.assertEqual(self.pipeline.load(wid), value)

    def test_material_selection_is_explicit_and_no_candidates_are_imported(self):
        with self.assertRaises(ValueError):
            self.pipeline.analyze(replace(self.request, material_ids=()))
        self.assertEqual(
            {m.material_id for m in self.pipeline.materials()},
            {"source:s", "source:url"},
        )
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0, 0))

    def test_processed_video_cites_exact_segment_and_preserves_selected_original_lineage(
        self,
    ):
        # Storage/graph fixture, not a claimed transcription/decode or video QA.
        original = self.root / "storage/originals/v.mp4"
        anchor = self.root / "storage/derived/a.wav"
        anchor.parent.mkdir(parents=True)
        original.write_bytes(b"graph-only original fixture")
        anchor.write_bytes(b"graph-only processed anchor fixture")
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db, db:
            db.execute(
                "INSERT INTO assets VALUES('video','VIDEO','Graph fixture','now',NULL)"
            )
            for fid, role, file, key, mime in (
                ("vid", "ORIGINAL", original, "storage/originals/v.mp4", "video/mp4"),
                ("aid", "DERIVED_AUDIO", anchor, "storage/derived/a.wav", "audio/wav"),
            ):
                db.execute(
                    "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,created_at,duration_ms) VALUES(?,'video',?,?,?,?,?,?,'now',2000)",
                    (
                        fid,
                        role,
                        key,
                        hashlib.sha256(file.read_bytes()).hexdigest(),
                        file.stat().st_size,
                        mime,
                        "vid" if fid == "aid" else None,
                    ),
                )
        request = replace(self.request, material_ids=("file:vid",))
        unprocessed = self.pipeline.analyze(request)
        self.assertIn(
            "UNPROCESSED_MEDIA", self.pipeline.load(unprocessed)["analysis_text"]
        )
        with self.assertRaises(ValueError):
            self.pipeline.create_draft(unprocessed)
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db, db:
            db.execute(
                "INSERT INTO evidences VALUES('speech','video','SPEECH','now',NULL)"
            )
            db.execute(
                "INSERT INTO evidence_versions VALUES('segment','speech','video',1,'aid','Kiểm chứng thông tin.','TIME_RANGE',?, 'HUMAN',NULL,'fixture','now')",
                (json.dumps({"start_ms": 0, "end_ms": 1000, "track": "audio"}),),
            )
        wid = self.pipeline.analyze(request)
        workflow = self.pipeline.load(wid)
        self.assertEqual(workflow["material_ids"], ["file:vid"])
        self.assertEqual(
            [(r["file_id"], r["evidence_version_id"]) for r in workflow["materials"]],
            [("vid", None), ("aid", "segment")],
        )
        self.assertIn('"end_ms": 1000', workflow["analysis_text"])
        self.assertIn("không phân tích toàn video", workflow["analysis_text"])
        self.assertTrue(self.pipeline.create_draft(wid).claim_version_ids)
