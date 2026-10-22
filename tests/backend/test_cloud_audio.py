"""Offline hostile protocol fixtures, not ASR/content/cloud acceptance."""

import copy
import hashlib
import io
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from creator_loop.backend.creator.web_domain import WebDomain
from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.knowledge.cloud_audio import (
    MAX_AUDIO_MS,
    WHISPER_MODEL,
    correct_cloud_audio_range,
    parse_whisper_transcript,
    prepare_cloud_transcription,
    probe_pcm_wav,
    store_cloud_transcript,
)
from creator_loop.backend.knowledge.evidence_reopen import reopen_evidence_version
from creator_loop.backend.runtime.database import _connect_write, initialize, validate


def wav(seconds=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * (seconds * 16000))
    return buffer.getvalue()


class CloudAudioTests(unittest.TestCase):
    def test_ieee_float32_original_is_bounded_without_transcoding(self):
        fmt = struct.pack("<HHIIHHH", 3, 1, 16000, 64000, 4, 32, 0)
        samples = struct.pack("<f", 0.25) * 16000
        chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
        chunks += b"fact" + struct.pack("<II", 4, 16000)
        chunks += b"data" + struct.pack("<I", len(samples)) + samples
        raw = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
        result = probe_pcm_wav(raw)
        self.assertEqual(result.duration_ms, 1000)
        self.assertEqual(result.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual((result.channels, result.sample_width), (1, 4))
        invalid = []
        for value in (float("nan"), float("inf"), 1.5):
            invalid.append(raw[:-4] + struct.pack("<f", value))
        invalid.append(raw[:46] + struct.pack("<I", 16001) + raw[50:])
        invalid.append(raw[:36] + b"\x01\0" + raw[38:])
        extra = raw[:38] + raw[38:50] + raw[38:]
        invalid.append(extra[:4] + struct.pack("<I", len(extra) - 8) + extra[8:])
        for value in invalid:
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                probe_pcm_wav(value)

    def setUp(self):
        self.raw = wav()
        self.probe = probe_pcm_wav(self.raw)
        self.response = {
            "text": "Đây là đoạn tiếng Việt.",
            "transcription_info": {"duration": 2, "language": "vi"},
            "segments": [
                {
                    "start": 0.1,
                    "end": 1.9,
                    "text": "Đây là đoạn tiếng Việt.",
                    "avg_logprob": -0.1,
                    "no_speech_prob": 0.01,
                    "compression_ratio": 1.0,
                }
            ],
        }

    def test_original_bytes_bound_actual_pcm_duration_without_decode_or_model(self):
        self.assertEqual(self.probe.duration_ms, 2000)
        self.assertEqual(self.probe.byte_size, len(self.raw))
        self.assertEqual((self.probe.channels, self.probe.sample_rate), (1, 16000))
        for raw in (
            b"ID3" + self.raw,
            self.raw[:-1],
            self.raw + b"hidden",
            wav(MAX_AUDIO_MS // 1000 + 1),
        ):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                probe_pcm_wav(raw)
        # Forged data chunk duration cannot be accepted from the extension/header.
        changed = bytearray(self.raw)
        changed[40:44] = (len(self.raw) * 2).to_bytes(4, "little")
        with self.assertRaisesRegex(ValueError, "thiếu byte"):
            probe_pcm_wav(bytes(changed))

    def test_multiple_decoder_ambiguous_chunks_cannot_hide_duration(self):
        for extra in (self.raw[12:36], self.raw[36:], b"xxxx\x00\x00\x00\x00"):
            altered = bytearray(self.raw + extra)
            altered[4:8] = (len(altered) - 8).to_bytes(4, "little")
            with self.subTest(chunk=extra[:4]), self.assertRaises(ValueError):
                probe_pcm_wav(bytes(altered))

    def test_conflicting_pcm_header_and_excessive_chunks_are_refused(self):
        for offset, value, size in ((28, 123, 4), (32, 3, 2), (34, 12, 2)):
            raw = bytearray(self.raw)
            raw[offset : offset + size] = value.to_bytes(size, "little")
            with (
                self.subTest(offset=offset),
                self.assertRaisesRegex(ValueError, "mâu thuẫn"),
            ):
                probe_pcm_wav(bytes(raw))
        raw = bytearray(self.raw + b"JUNK\x00\x00\x00\x00" * 256)
        raw[4:8] = (len(raw) - 8).to_bytes(4, "little")
        with self.assertRaisesRegex(ValueError, "256"):
            probe_pcm_wav(bytes(raw))

    def test_wrong_task_or_deleted_asset_refuses_callback(self):
        root, db, file = self.storage_fixture()
        with db:
            db.execute("UPDATE processing_runs SET task_type='CREATOR_GENERATION'")
        with self.assertRaisesRegex(ValueError, "không khớp"):
            store_cloud_transcript(
                db,
                data_root=root,
                file_id=file["file_id"],
                run_id="fixture-run",
                native_response=self.response,
            )
        with db:
            db.execute("UPDATE processing_runs SET task_type='SOURCE_TRANSCRIPTION'")
            db.execute("UPDATE assets SET deleted_at=?", (_timestamp(),))
        with self.assertRaisesRegex(ValueError, "không khớp"):
            store_cloud_transcript(
                db,
                data_root=root,
                file_id=file["file_id"],
                run_id="fixture-run",
                native_response=self.response,
            )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], 0
        )

    def test_transcript_keeps_native_response_and_never_approves(self):
        before = copy.deepcopy(self.response)
        transcript = parse_whisper_transcript(self.response, self.probe)
        self.assertEqual(self.response, before)
        self.assertEqual(transcript.review_state, "PENDING")
        self.assertEqual(transcript.segments[0].start_ms, 100)
        self.assertEqual(transcript.segments[0].end_ms, 1900)
        self.assertEqual(transcript.warnings, ())
        self.assertEqual(transcript.text, self.response["text"])

    def test_uncertain_text_is_visible_and_missing_metrics_are_not_confidence(self):
        segment = self.response["segments"][0]
        segment.update(avg_logprob=-1.1, no_speech_prob=0.9, compression_ratio=3)
        transcript = parse_whisper_transcript(self.response, self.probe)
        self.assertEqual(len(transcript.segments[0].warnings), 3)
        self.assertEqual(transcript.segments[0].text, segment["text"])
        for key in ("avg_logprob", "no_speech_prob", "compression_ratio"):
            del segment[key]
        transcript = parse_whisper_transcript(self.response, self.probe)
        self.assertEqual(len(transcript.segments[0].warnings), 3)
        self.assertTrue(
            all(w.startswith("MISSING_") for w in transcript.segments[0].warnings)
        )

    def test_bad_timing_quantity_and_nonfinite_scores_fail_closed(self):
        for change in (
            {"start": True},
            {"start": -0.0001},
            {"end": 2.1},
            {"end": float("nan")},
            {"no_speech_prob": 1.1},
            {"avg_logprob": 1},
        ):
            response = copy.deepcopy(self.response)
            response["segments"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_whisper_transcript(response, self.probe)
        self.response["transcription_info"]["duration"] = 100
        with self.assertRaisesRegex(ValueError, "không khớp"):
            parse_whisper_transcript(self.response, self.probe)

    def test_unlocated_full_text_is_not_silently_dropped_or_called_aligned(self):
        self.response["text"] += " Câu chưa có thời gian."
        transcript = parse_whisper_transcript(self.response, self.probe)
        self.assertIn("FULL_TEXT_SEGMENT_MISMATCH", transcript.warnings)
        self.assertIn("chưa có thời gian", transcript.text)
        self.assertEqual(transcript.review_state, "PENDING")

    def test_text_without_timed_segments_is_not_timed_source_evidence(self):
        del self.response["segments"]
        with self.assertRaisesRegex(ValueError, "thời gian"):
            parse_whisper_transcript(self.response, self.probe)

    def test_uncovered_audio_is_explicit_not_silently_labelled_silence(self):
        self.response["segments"][0].update(start=0.6, end=1.1)
        transcript = parse_whisper_transcript(self.response, self.probe)
        self.assertEqual(transcript.untranscribed_ranges, ((0, 600), (1100, 2000)))
        self.assertIn("UNTRANSCRIBED_RANGES", transcript.warnings)
        self.assertEqual(transcript.review_state, "PENDING")

    def storage_fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        initialize(root / "creator_loop.sqlite3")
        domain = WebDomain(root)
        project = domain.project("Isolated audio fixture")["project_id"]
        file = domain.intake(
            {
                "project_id": project,
                "media_type": "AUDIO",
                "display_name": "fixture.wav",
            },
            self.raw,
        )
        db = _connect_write(domain.path)
        self.addCleanup(db.close)
        with db:
            db.execute(
                """INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,model_name,model_version,started_at,created_at)
                VALUES('fixture-run',?,?,'SOURCE_TRANSCRIPTION','RUNNING','CLOUDFLARE_WORKERS_AI_SOURCE','fixture',?,'fixture',?,?)""",
                (
                    file["asset_id"],
                    file["file_id"],
                    WHISPER_MODEL,
                    _timestamp(),
                    _timestamp(),
                ),
            )
        return root, db, file

    def test_prepare_source_run_binds_original_and_refuses_replay_without_inference(
        self,
    ):
        root, db, file = self.storage_fixture()
        key = "a" * 32
        packet = prepare_cloud_transcription(
            db, data_root=root, file_id=file["file_id"], run_id=key, version="fixture"
        )
        self.assertEqual(packet["input_sha256"], self.probe.sha256)
        self.assertEqual(packet["duration_ms"], 2000)
        self.assertEqual(
            db.execute(
                "SELECT status,task_type FROM processing_runs WHERE run_id=?", (key,)
            ).fetchone(),
            ("QUEUED", "SOURCE_TRANSCRIPTION"),
        )
        with self.assertRaisesRegex(ValueError, "đã tồn tại"):
            prepare_cloud_transcription(
                db,
                data_root=root,
                file_id=file["file_id"],
                run_id=key,
                version="fixture",
            )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], 0
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
        )
        validate(db)

    def test_web_correction_is_project_bound_and_reopens_exact_timed_version(self):
        root, db, file = self.storage_fixture()
        domain = WebDomain(root)
        project = domain.pipeline.projects()[0].project_id
        other = domain.project("Unrelated Project")["project_id"]
        result = store_cloud_transcript(
            db,
            data_root=root,
            file_id=file["file_id"],
            run_id="fixture-run",
            native_response=self.response,
        )
        data = {
            "project_id": project,
            "file_id": file["file_id"],
            "evidence_version_id": result["evidence_version_ids"][0],
            "start_ms": 200,
            "end_ms": 1600,
            "content": "Lời nói đã đối chiếu; tên riêng chưa rõ.",
            "reason": "Nghe lại đúng khoảng thời gian",
        }
        with self.assertRaisesRegex(ValueError, "liên kết"):
            domain.source_audio_range(data | {"project_id": other})
        with self.assertRaisesRegex(ValueError, "thuộc nguồn"):
            domain.source_audio_range(data | {"evidence_version_id": "missing"})
        corrected = domain.source_audio_range(data)
        self.assertEqual(corrected["review_state"], "PENDING")
        reopened = WebDomain(root).source_review(project, file["file_id"])
        self.assertEqual(len(reopened["evidence_versions"]), 2)
        current = reopened["evidence_versions"][-1]
        self.assertEqual(current["content"], data["content"])
        self.assertEqual(
            json.loads(current["locator_data"]),
            {
                "start_ms": 200,
                "end_ms": 1600,
                "track": "audio",
            },
        )
        self.assertEqual(current["producer_type"], "HUMAN")
        self.assertIsNone(reopened["source_use"])
        with self.assertRaisesRegex(ValueError, "hiện tại"):
            domain.source_audio_range(data)
        validate(db)

    def test_bound_model_evidence_keeps_native_artifact_without_any_approval(self):
        root, db, file = self.storage_fixture()
        before = copy.deepcopy(self.response)
        result = store_cloud_transcript(
            db,
            data_root=root,
            file_id=file["file_id"],
            run_id="fixture-run",
            native_response=self.response,
        )
        artifact = json.loads(result["content"])
        self.assertEqual(artifact["native_response"], before)
        self.assertEqual((root / result["storage_key"]).read_bytes(), result["content"])
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM project_material_use_versions").fetchone()[
                0
            ],
            0,
        )
        version = result["evidence_version_ids"][0]
        reopened = reopen_evidence_version(db, version, root)
        self.assertEqual(
            reopened.locator, {"start_ms": 100, "end_ms": 1900, "track": "audio"}
        )
        self.assertEqual(
            db.execute(
                "SELECT producer_type,processing_run_id FROM evidence_versions"
            ).fetchone(),
            ("MODEL", "fixture-run"),
        )
        validate(db)
        with self.assertRaisesRegex(ValueError, "không khớp"):
            store_cloud_transcript(
                db,
                data_root=root,
                file_id=file["file_id"],
                run_id="fixture-run",
                native_response=self.response,
            )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], 1
        )

    def test_bad_provenance_or_original_cannot_write_model_evidence(self):
        root, db, file = self.storage_fixture()
        with self.assertRaisesRegex(ValueError, "không khớp"):
            store_cloud_transcript(
                db,
                data_root=root,
                file_id=file["file_id"],
                run_id="other-run",
                native_response=self.response,
            )
        original = (
            root
            / db.execute(
                "SELECT storage_key FROM asset_files WHERE file_id=?",
                (file["file_id"],),
            ).fetchone()[0]
        )
        original.write_bytes(self.raw[:-1])
        with self.assertRaisesRegex(ValueError, "đã thay đổi"):
            store_cloud_transcript(
                db,
                data_root=root,
                file_id=file["file_id"],
                run_id="fixture-run",
                native_response=self.response,
            )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], 0
        )
        self.assertEqual(
            db.execute(
                "SELECT COUNT(*) FROM asset_files WHERE role='OTHER'"
            ).fetchone()[0],
            0,
        )

    def test_user_correction_keeps_model_original_and_invalidates_old_source_choice(
        self,
    ):
        from creator_loop.backend.knowledge.source_use import (
            permitted_source_versions,
            select_source_use,
        )

        root, db, file = self.storage_fixture()
        result = store_cloud_transcript(
            db,
            data_root=root,
            file_id=file["file_id"],
            run_id="fixture-run",
            native_response=self.response,
        )
        old = result["evidence_version_ids"][0]
        project = db.execute("SELECT project_id FROM projects").fetchone()[0]
        select_source_use(
            db,
            data_root=root,
            project_id=project,
            file_id=file["file_id"],
            state="SELECTED",
            evidence_version_ids=[old],
            expected_parent=None,
            actor="QA fixture",
        )
        new = correct_cloud_audio_range(
            db,
            data_root=root,
            evidence_version_id=old,
            start_ms=200,
            end_ms=1800,
            content="Bản người dùng sửa.",
            actor="QA fixture",
            reason="Đã nghe đối chiếu",
        )
        self.assertNotEqual(new, old)
        self.assertEqual(
            db.execute(
                "SELECT content,producer_type FROM evidence_versions WHERE evidence_version_id=?",
                (old,),
            ).fetchone(),
            (self.response["text"], "MODEL"),
        )
        self.assertEqual(
            db.execute(
                "SELECT content,producer_type FROM evidence_versions WHERE evidence_version_id=?",
                (new,),
            ).fetchone(),
            ("Bản người dùng sửa.", "HUMAN"),
        )
        self.assertEqual(
            db.execute("SELECT action FROM review_events").fetchall(), [("CORRECT",)]
        )
        self.assertEqual(
            reopen_evidence_version(db, new, root).locator["start_ms"], 200
        )
        with self.assertRaisesRegex(ValueError, "phiên bản hiện tại"):
            permitted_source_versions(
                db, data_root=root, project_id=project, file_id=file["file_id"]
            )
        with self.assertRaisesRegex(ValueError, "hiện tại"):
            correct_cloud_audio_range(
                db,
                data_root=root,
                evidence_version_id=old,
                start_ms=200,
                end_ms=1800,
                content="stale",
                actor="QA fixture",
                reason="stale",
            )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], 2
        )
        validate(db)
