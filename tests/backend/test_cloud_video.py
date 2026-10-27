"""Hostile source protocol fixtures, not a decoder/cloud/video quality proof."""

import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from creator_loop.backend.creator.web_domain import WebDomain
from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.knowledge.cloud_audio import WHISPER_MODEL
from creator_loop.backend.knowledge.cloud_image import IMAGE_MODEL
from creator_loop.backend.knowledge.cloud_video import (
    VIDEO_MODEL,
    correct_cloud_video_range,
    parse_video_result,
    prepare_cloud_video,
    store_cloud_video,
)
from creator_loop.backend.runtime.database import _connect_write, initialize, validate


class CloudVideoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        initialize(self.root / "creator_loop.sqlite3")
        self.domain = WebDomain(self.root)
        self.project = self.domain.project("Video isolation")["project_id"]
        self.other = self.domain.project("Unlinked Project")["project_id"]
        self.raw = b"\x00\x00\x00\x18ftypisom" + b"\0" * 32
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.file = self.domain.intake(
            {
                "project_id": self.project,
                "media_type": "VIDEO",
                "display_name": "Nguồn video.mp4",
            },
            self.raw,
        )["file_id"]
        self.run = "a" * 32
        self.response = {
            "input_sha256": self.sha,
            "decoder": {
                "duration_ms": 2000,
                "audio_duration_ms": 2000,
                "audio_sha256": "b" * 64,
                "frames": [{"at_ms": 0, "end_ms": 1, "sha256": "c" * 64}],
            },
            "audio": {
                "model": WHISPER_MODEL,
                "native_response": {
                    "text": "Một nguồn thử.",
                    "transcription_info": {"duration": 2},
                    "segments": [
                        {
                            "start": 0.1,
                            "end": 1.9,
                            "text": "Một nguồn thử.",
                            "avg_logprob": -0.1,
                            "no_speech_prob": 0.01,
                            "compression_ratio": 1,
                        }
                    ],
                },
            },
            "vision": {
                "model": IMAGE_MODEL,
                "native_response": {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "frames": [
                                            {
                                                "at_ms": 0,
                                                "description": "Một vật màu xanh.",
                                                "visible_text": "",
                                                "uncertain": ["Chưa rõ vật gì"],
                                                "unread": [],
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            },
                        }
                    ]
                },
            },
        }

    def store(self):
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            prepare_cloud_video(
                db,
                data_root=self.root,
                file_id=self.file,
                run_id=self.run,
                version="fixture",
            )
            db.execute(
                "UPDATE processing_runs SET status='RUNNING',started_at=? WHERE run_id=?",
                (_timestamp(), self.run),
            )
            db.commit()
            return store_cloud_video(
                db,
                data_root=self.root,
                file_id=self.file,
                run_id=self.run,
                result=self.response,
            )

    def test_original_bound_tracks_pending_and_original_reopen(self):
        saved = self.store()
        review = self.domain.source_review(self.project, self.file)
        self.assertEqual(len(saved["evidence_version_ids"]), 2)
        self.assertEqual(
            {
                json.loads(v["locator_data"])["track"]
                for v in review["evidence_versions"]
            },
            {"audio", "video"},
        )
        self.assertTrue(
            all(
                v["producer_type"] == "MODEL" and v["review_state"] == "PENDING"
                for v in review["evidence_versions"]
            )
        )
        self.assertIsNone(review["source_use"])
        self.assertFalse(review["publication_approval"])
        self.assertEqual(
            (self.root / "storage/originals" / self.sha).read_bytes(), self.raw
        )
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                db.execute(
                    "SELECT status,model_name FROM processing_runs WHERE run_id=?",
                    (self.run,),
                ).fetchone(),
                ("SUCCEEDED", VIDEO_MODEL),
            )
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            validate(db)
            with self.assertRaises(ValueError):
                prepare_cloud_video(
                    db,
                    data_root=self.root,
                    file_id=self.file,
                    run_id="e" * 32,
                    version="fixture",
                )

    def test_missing_or_wrong_frame_timing_never_becomes_complete(self):
        for mutate in (
            lambda x: x["decoder"].update(frames=[]),
            lambda x: x["decoder"]["frames"][0].update(at_ms=1),
            lambda x: x.update(input_sha256="d" * 64),
            lambda x: x["audio"].update(model=IMAGE_MODEL),
            lambda x: x["decoder"].update(duration_ms=30001),
            lambda x: x["vision"]["native_response"]["choices"][0].update(
                finish_reason="length"
            ),
        ):
            result = copy.deepcopy(self.response)
            mutate(result)
            with self.subTest(result=result), self.assertRaises(ValueError):
                parse_video_result(result, self.sha)

    def test_correction_keeps_raw_model_version_without_approval(self):
        self.store()
        old = self.domain.source_review(self.project, self.file)["evidence_versions"][0]
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            vid = correct_cloud_video_range(
                db,
                data_root=self.root,
                evidence_version_id=old["evidence_version_id"],
                start_ms=0,
                end_ms=1,
                content="Quan sát đã sửa (fixture).",
                actor="QA_FIXTURE",
                reason="Protocol test, not human approval",
            )
        review = self.domain.source_review(self.project, self.file)
        self.assertTrue(
            any(
                v["evidence_version_id"] == old["evidence_version_id"]
                and v["content"] == old["content"]
                for v in review["evidence_versions"]
            )
        )
        self.assertTrue(
            any(
                v["evidence_version_id"] == vid
                and v["version_no"] == 2
                and v["review_state"] == "PENDING"
                for v in review["evidence_versions"]
            )
        )
        self.assertFalse(review["factual_approval"])
        self.assertFalse(review["publication_approval"])

    def test_wrong_project_and_stale_correction_refuse(self):
        self.store()
        old = self.domain.source_review(self.project, self.file)["evidence_versions"][0]
        with self.assertRaises(ValueError):
            self.domain.source_audio_range(
                {
                    "project_id": self.other,
                    "file_id": self.file,
                    "evidence_version_id": old["evidence_version_id"],
                    "start_ms": 0,
                    "end_ms": 1,
                    "content": "Không được sửa",
                    "reason": "Unlinked",
                }
            )
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            kwargs = dict(
                data_root=self.root,
                evidence_version_id=old["evidence_version_id"],
                start_ms=0,
                end_ms=1,
                content="Bản sửa.",
                actor="QA_FIXTURE",
                reason="Test",
            )
            correct_cloud_video_range(db, **kwargs)
            with self.assertRaises(ValueError):
                correct_cloud_video_range(db, **kwargs)

    def test_failure_is_atomic_and_successful_read_not_reenqueued(self):
        self.response["vision"]["native_response"]["choices"][0]["message"][
            "content"
        ] = '{"frames":[]}'
        with self.assertRaises(ValueError):
            self.store()
        self.assertEqual(
            self.domain.source_review(self.project, self.file)["evidence_versions"], []
        )


if __name__ == "__main__":
    unittest.main()
