"""Offline image protocol/version fixtures; not real vision or cloud acceptance."""

import copy
import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from creator_loop.backend.creator.web_domain import WebDomain
from creator_loop.backend.knowledge.cloud_image import (
    IMAGE_MODEL,
    correct_cloud_image,
    parse_image_observation,
    prepare_cloud_image,
    probe_image,
    store_cloud_image,
)
from creator_loop.backend.knowledge.evidence_reopen import reopen_evidence_version
from creator_loop.backend.runtime.database import _connect_write, initialize, validate


def png(width=2, height=2):
    def chunk(kind, body):
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\x20\x60\x80" * width) * height))
        + chunk(b"IEND", b"")
    )


def response():
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "description": "Mô tả fixture, chưa đối chiếu ảnh thật.",
                            "visible_text": "CHỈ KIỂM GIAO THỨC",
                            "uncertain": ["Nội dung máy cần kiểm."],
                            "unread": ["Không chứng minh nguồn ảnh thật."],
                        },
                        ensure_ascii=False,
                    )
                },
            }
        ],
        "usage": None,
    }


class CloudImageTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        initialize(root / "creator_loop.sqlite3")
        domain = WebDomain(root)
        project = domain.project("Isolated image source")["project_id"]
        file = domain.intake(
            {
                "project_id": project,
                "media_type": "IMAGE",
                "display_name": "Ảnh có dấu khoảng trắng.png",
                "source": {"rights": "OWNED"},
            },
            png(),
        )
        db = _connect_write(domain.path)
        self.addCleanup(db.close)
        return root, domain, project, file, db

    def test_header_probe_keeps_original_and_rejects_size_crc_animation_or_ambiguity(
        self,
    ):
        raw = png()
        p = probe_image(raw)
        self.assertEqual(
            (p.encoded_width, p.encoded_height, p.mime_type), (2, 2, "image/png")
        )
        self.assertEqual(p.sha256, hashlib.sha256(raw).hexdigest())
        for value in (
            raw[:-1],
            raw + b"extra",
            raw[:29] + b"\0\0\0\0" + raw[33:],
            b"SVG" + raw,
            b"0" * (8 * 1024 * 1024 + 1),
        ):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                probe_image(value)
        ac = b"acTL" + struct.pack(">II", 2, 0)
        animated = (
            raw[:33]
            + struct.pack(">I", 8)
            + ac
            + struct.pack(">I", zlib.crc32(ac))
            + raw[33:]
        )
        with self.assertRaisesRegex(ValueError, "Ảnh động"):
            probe_image(animated)

    def test_supported_jpeg_and_webp_headers_are_probes_not_decode_claims(self):
        jpeg = (
            b"\xff\xd8\xff\xc0"
            + struct.pack(">H", 8)
            + b"\x08"
            + struct.pack(">HH", 3, 4)
            + b"\0"
            + b"\xff\xda\0\x02"
            + b"0" * 10
            + b"\xff\xd9"
        )
        # Deliberately only a protocol header fixture, never passed to real AI.
        p = probe_image(jpeg)
        self.assertEqual((p.encoded_width, p.encoded_height), (4, 3))
        with self.assertRaises(ValueError):
            probe_image(jpeg + jpeg)
        payload = b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", 4, 3)
        chunk = b"VP8 " + struct.pack("<I", len(payload)) + payload
        webp = b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk
        self.assertEqual(probe_image(webp).mime_type, "image/webp")
        with self.assertRaises(ValueError):
            probe_image(webp + b"x")

    def test_native_json_is_retained_strictly_without_guessed_text_or_approval(self):
        native = response()
        saved = copy.deepcopy(native)
        parsed = parse_image_observation(native)
        self.assertEqual(native, saved)
        self.assertEqual(parsed.review_state, "PENDING")
        self.assertIn("CHỈ KIỂM", parsed.text)
        self.assertTrue(parsed.uncertain and parsed.unread)
        for change in (
            "not json",
            '{"description":"x","description":"y","visible_text":"","uncertain":[],"unread":[]}',
            json.dumps(
                {
                    "description": "x",
                    "visible_text": "",
                    "uncertain": [],
                    "unread": [],
                    "approved": True,
                }
            ),
        ):
            invalid = response()
            invalid["choices"][0]["message"]["content"] = change
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_image_observation(invalid)
        invalid = response()
        invalid["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(ValueError):
            parse_image_observation(invalid)

    def test_model_run_preserves_native_versions_hashes_and_human_source_choice(self):
        root, domain, project, file, db = self.fixture()
        run = "c" * 32
        packet = prepare_cloud_image(
            db,
            data_root=root,
            file_id=file["file_id"],
            run_id=run,
            version="fixture-source",
        )
        self.assertEqual(packet["model"], IMAGE_MODEL)
        with db:
            db.execute(
                "UPDATE processing_runs SET status='RUNNING', "
                "started_at='2026-10-23T00:00:00Z' WHERE run_id=?",
                (run,),
            )
        native = response()
        result = store_cloud_image(
            db,
            data_root=root,
            file_id=file["file_id"],
            run_id=run,
            native_response=native,
        )
        artifact = json.loads(result["content"])
        self.assertEqual(artifact["native_response"], native)
        self.assertFalse(
            artifact["factual_approval"] or artifact["publication_approval"]
        )
        self.assertEqual(len(result["evidence_version_ids"]), 2)
        old = result["evidence_version_ids"][0]
        self.assertEqual(
            reopen_evidence_version(db, old, root).anchor_file_id, file["file_id"]
        )
        choice = domain.source_use(
            {
                "project_id": project,
                "file_id": file["file_id"],
                "state": "SELECTED",
                "evidence_version_ids": [old],
                "expected_parent": None,
                "explicit_source_use_choice": True,
                "reason": "Isolated QA source choice, not human factual approval",
            }
        )
        self.assertFalse(choice["factual_approval"] or choice["publication_approval"])
        newer = correct_cloud_image(
            db,
            data_root=root,
            evidence_version_id=old,
            region={"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.5},
            content="Nội dung sửa fixture, không phải output AI gốc.",
            actor="QA_FIXTURE",
            reason="Protocol correction only",
        )
        versions = domain.source_review(project, file["file_id"])["evidence_versions"]
        self.assertIn(old, {v["evidence_version_id"] for v in versions})
        v = next(v for v in versions if v["evidence_version_id"] == newer)
        self.assertEqual(
            (v["version_no"], v["producer_type"], v["review_state"]),
            (2, "HUMAN", "PENDING"),
        )
        self.assertFalse(domain.state(project)["materials"][0]["usable_for_draft"])
        with self.assertRaisesRegex(ValueError, "hiện tại"):
            correct_cloud_image(
                db,
                data_root=root,
                evidence_version_id=old,
                region={"x": 0, "y": 0, "width": 1, "height": 1},
                content="Stale edit must not append a second branch",
                actor="QA_FIXTURE",
                reason="Old version race",
            )
        for action in ("ACCEPT", "APPROVE"):
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM review_events WHERE action=?", (action,)
                ).fetchone()[0],
                0,
            )
        validate(db, expected_version=11)
        with self.assertRaisesRegex(ValueError, "Đã có phân tích"):
            prepare_cloud_image(
                db,
                data_root=root,
                file_id=file["file_id"],
                run_id="d" * 32,
                version="fixture-source",
            )

    def test_wrong_project_stale_version_invalid_region_and_changed_original_fail(self):
        root, domain, project, file, db = self.fixture()
        run = "e" * 32
        domain.begin_source_image(
            {"project_id": project, "file_id": file["file_id"]}, run, "fixture"
        )
        with db:
            db.execute(
                "UPDATE processing_runs SET status='RUNNING', "
                "started_at='2026-10-23T00:00:00Z' WHERE run_id=?",
                (run,),
            )
        result = store_cloud_image(
            db,
            data_root=root,
            file_id=file["file_id"],
            run_id=run,
            native_response=response(),
        )
        old = result["evidence_version_ids"][0]
        other = domain.project("Other")["project_id"]
        with self.assertRaisesRegex(ValueError, "liên kết"):
            domain.source_image_region(
                {
                    "project_id": other,
                    "file_id": file["file_id"],
                    "evidence_version_id": old,
                }
            )
        for region in (
            {"x": 0, "y": 0, "width": float("nan"), "height": 1},
            {"x": 0.8, "y": 0, "width": 0.5, "height": 1},
        ):
            with self.assertRaises(ValueError):
                correct_cloud_image(
                    db,
                    data_root=root,
                    evidence_version_id=old,
                    region=region,
                    content="x",
                    actor="fixture",
                    reason="fixture",
                )
        original = root / "storage/originals" / file["sha256"]
        original.write_bytes(png(3, 3))
        before = db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0]
        with self.assertRaises(ValueError):
            correct_cloud_image(
                db,
                data_root=root,
                evidence_version_id=old,
                region={"x": 0, "y": 0, "width": 1, "height": 1},
                content="x",
                actor="fixture",
                reason="fixture",
            )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], before
        )
