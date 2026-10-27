"""Cloud video source protocol: original-bound speech and sampled visuals.

This module does not decode, infer, grant source permission or approve content.
Decoding and both native calls belong to the durable cloud job.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.knowledge.cloud_audio import (
    WHISPER_MODEL,
    CloudAudioProbe,
    parse_whisper_transcript,
)
from creator_loop.backend.knowledge.cloud_image import IMAGE_MODEL, _plain
from creator_loop.backend.knowledge.evidence import (
    Evidence,
    EvidenceRepository,
    EvidenceVersion,
)
from creator_loop.backend.knowledge.evidence_reopen import _anchor_path
from creator_loop.backend.runtime.transactions import atomic_transaction

VIDEO_MODEL = "CREATOR_VIDEO_SOURCE_V1"
MAX_VIDEO_MS = 30_000


def original_video(db: sqlite3.Connection, root: Path, file_id: str):
    row = db.execute(
        """SELECT f.asset_id,f.storage_key,f.sha256,f.byte_size FROM asset_files f
        JOIN assets a USING(asset_id) WHERE f.file_id=? AND f.role='ORIGINAL'
        AND a.media_type='VIDEO' AND a.deleted_at IS NULL""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Cần video gốc còn hiệu lực trong Project")
    raw = _anchor_path(root, "ORIGINAL", row[1]).read_bytes()
    if (
        not 16 <= len(raw) <= 8 * 1024 * 1024
        or (hashlib.sha256(raw).hexdigest(), len(raw)) != row[2:]
    ):
        raise ValueError("Byte video không khớp hoặc vượt 8 MiB")
    if raw[4:8] != b"ftyp" and raw[:4] != b"\x1a\x45\xdf\xa3":
        raise ValueError(
            "Đọc video hỗ trợ MP4/WebM; codec/thời lượng còn phải được kiểm trên cloud"
        )
    return row[0], raw, row[2]


def prepare_cloud_video(
    db: sqlite3.Connection, *, data_root: Path, file_id: str, run_id: str, version: str
):
    if not re.fullmatch("[a-f0-9]{32}", run_id) or not version.strip():
        raise ValueError("Run/version video không hợp lệ")
    with atomic_transaction(db):
        asset_id, _, sha = original_video(db, data_root, file_id)
        if db.execute(
            "SELECT 1 FROM processing_runs WHERE input_file_id=? AND tool_name='CLOUDFLARE_WORKERS_AI_SOURCE' AND model_name=? AND status='SUCCEEDED'",
            (file_id, VIDEO_MODEL),
        ).fetchone():
            raise ValueError(
                "Đã có kết quả đọc video; mở nguồn để đối chiếu/sửa, không gọi lại"
            )
        db.execute(
            """INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,model_name,model_version,created_at)
        VALUES(?,?,?,'SOURCE_VIDEO_ANALYSIS','QUEUED','CLOUDFLARE_WORKERS_AI_SOURCE',?,?,?,?)""",
            (run_id, asset_id, file_id, version, VIDEO_MODEL, version, _timestamp()),
        )
        return {"request_id": run_id, "input_sha256": sha, "model": VIDEO_MODEL}


def parse_video_result(result: Any, original_sha: str):
    if not isinstance(result, dict) or result.get("input_sha256") != original_sha:
        raise ValueError("Video result không gắn đúng bản gốc")
    decoder = result.get("decoder", {})
    duration = decoder.get("duration_ms")
    audio_duration = decoder.get("audio_duration_ms")
    if (
        type(duration) is not int
        or not 1 <= duration <= MAX_VIDEO_MS
        or type(audio_duration) is not int
        or abs(audio_duration - duration) > 250
    ):
        raise ValueError("Thời lượng cloud audio/video không khớp")
    frames = decoder.get("frames")
    expected = list(range(0, duration, 2000))
    if (
        not isinstance(frames, list)
        or [f.get("at_ms") for f in frames] != expected
        or len(expected) > 15
    ):
        raise ValueError("Thiếu mẫu hình ảnh video; không nhận kết quả rỗng")
    if any(
        set(f) != {"at_ms", "end_ms", "sha256"}
        or f["end_ms"] != min(duration, f["at_ms"] + 1)
        or not re.fullmatch("[a-f0-9]{64}", f["sha256"])
        for f in frames
    ):
        raise ValueError("Frame locator/hash không hợp lệ")
    audio = result.get("audio", {})
    vision = result.get("vision", {})
    if audio.get("model") != WHISPER_MODEL or vision.get("model") != IMAGE_MODEL:
        raise ValueError("Video dùng sai speech/vision model")
    # Original video hash remains the anchor; this probe describes the transient
    # decoded PCM, whose independent hash and decoder receipt are retained.
    audio_sha = decoder.get("audio_sha256", "")
    if not re.fullmatch("[a-f0-9]{64}", audio_sha):
        raise ValueError("Thiếu digest audio đã giải mã")
    transcript = parse_whisper_transcript(
        audio.get("native_response"),
        CloudAudioProbe(
            audio_sha, 44 + audio_duration * 32, audio_duration, 16000, 1, 2
        ),
    )
    native = vision.get("native_response", {})
    choices = native.get("choices", [])
    if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
        raise ValueError("Đọc hình ảnh video chưa kết thúc")
    raw = choices[0].get("message", {}).get("content")
    if not isinstance(raw, str) or len(raw.encode()) > 128 * 1024:
        raise ValueError("Video vision output không hợp lệ")

    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("Video JSON có trường trùng")
        return value

    visual = json.loads(raw, object_pairs_hook=unique)
    if (
        not isinstance(visual, dict)
        or set(visual) != {"frames"}
        or not isinstance(visual["frames"], list)
        or len(visual["frames"]) != len(expected)
    ):
        raise ValueError("Vision phải báo từng khung hình đã lấy mẫu")
    observations = []
    for f, at in zip(visual["frames"], expected):
        if (
            not isinstance(f, dict)
            or set(f) != {"at_ms", "description", "visible_text", "uncertain", "unread"}
            or type(f["at_ms"]) is not int
            or f["at_ms"] != at
        ):
            raise ValueError("Vision timestamp/fields không khớp mẫu")
        item = {
            "at_ms": at,
            "end_ms": min(duration, at + 1),
            "description": _plain(f["description"], 4000),
            "visible_text": _plain(f["visible_text"], 4000, empty=True),
        }
        for k in ("uncertain", "unread"):
            if not isinstance(f[k], list) or len(f[k]) > 16:
                raise ValueError("Thiếu phạm vi chưa đọc video")
            item[k] = [_plain(v, 1000) for v in f[k]]
        observations.append(item)
    return {
        "text": transcript.text,
        "segments": asdict(transcript)["segments"],
        "warnings": list(transcript.warnings),
        "untranscribed_ranges": list(transcript.untranscribed_ranges),
        "visual_frames": observations,
        "visual_sampling_ms": 2000,
        "unread": ["Hình ảnh giữa các mốc lấy mẫu 2 giây chưa được đọc."],
        "review_state": "PENDING",
    }


def store_cloud_video(
    db: sqlite3.Connection, *, data_root: Path, file_id: str, run_id: str, result: Any
):
    with atomic_transaction(db):
        asset_id, _, original_sha = original_video(db, data_root, file_id)
        if db.execute(
            "SELECT status,model_name FROM processing_runs WHERE run_id=? AND input_file_id=? AND task_type='SOURCE_VIDEO_ANALYSIS'",
            (run_id, file_id),
        ).fetchone() != ("RUNNING", VIDEO_MODEL):
            raise ValueError("Video callback không khớp run/model đang chạy")
        parsed = parse_video_result(result, original_sha)
        now = _timestamp()
        repo = EvidenceRepository(db)
        ids = []
        db.execute(
            "UPDATE asset_files SET duration_ms=? WHERE file_id=?",
            (result["decoder"]["duration_ms"], file_id),
        )
        contents = [
            ("SPEECH", s["text"], s["start_ms"], s["end_ms"], "audio")
            for s in parsed["segments"]
        ]
        for f in parsed["visual_frames"]:
            contents.append(
                (
                    "VISUAL_OBSERVATION",
                    f["description"],
                    f["at_ms"],
                    f["end_ms"],
                    "video",
                )
            )
            if f["visible_text"]:
                contents.append(
                    ("OCR_TEXT", f["visible_text"], f["at_ms"], f["end_ms"], "video")
                )
        for kind, text, start, end, track in contents:
            eid, vid = uuid4().hex, uuid4().hex
            ids.append(vid)
            repo.create_with_version(
                Evidence(eid, asset_id, kind, now, None),
                EvidenceVersion(
                    vid,
                    eid,
                    asset_id,
                    1,
                    file_id,
                    text,
                    "TIME_RANGE",
                    json.dumps(
                        {"start_ms": start, "end_ms": end, "track": track},
                        sort_keys=True,
                    ),
                    "MODEL",
                    run_id,
                    None,
                    now,
                ),
            )
        artifact = {
            "format": "CREATOR_CLOUD_VIDEO_V1",
            "model": VIDEO_MODEL,
            "run_id": run_id,
            "input_file_id": file_id,
            "input_sha256": original_sha,
            "native_result": result,
            "parsed": parsed,
            "evidence_version_ids": ids,
            "factual_approval": False,
            "publication_approval": False,
        }
        blob = json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(blob) > 256 * 1024:
            raise ValueError("Video artifact vượt256KiB; không lưu dở")
        sha = hashlib.sha256(blob).hexdigest()
        key = "storage/derived/" + sha
        path = _anchor_path(data_root, "OTHER", key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        db.execute(
            """INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,processing_run_id,created_at)
        VALUES(?,?,'OTHER',?,?,?,'application/json',?,?,?)""",
            (uuid4().hex, asset_id, key, sha, len(blob), file_id, run_id, now),
        )
        db.execute(
            "UPDATE processing_runs SET status='SUCCEEDED',finished_at=? WHERE run_id=?",
            (now, run_id),
        )
        return {"sha256": sha, "content": blob, "evidence_version_ids": ids}


def correct_cloud_video_range(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    evidence_version_id: str,
    start_ms: int,
    end_ms: int,
    content: str,
    actor: str,
    reason: str,
):
    text = _plain(content, 12000)
    reason = _plain(reason, 2000)
    actor = _plain(actor, 200)
    with atomic_transaction(db):
        old = db.execute(
            """SELECT v.evidence_id,v.asset_id,v.anchor_file_id,v.version_no,v.locator_type,v.locator_data,
        (SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=v.evidence_id)
        FROM evidence_versions v JOIN evidences e USING(evidence_id) WHERE v.evidence_version_id=? AND e.deleted_at IS NULL""",
            (evidence_version_id,),
        ).fetchone()
        if old is None or old[4] != "TIME_RANGE" or old[3] != old[6]:
            raise ValueError("Chỉ sửa mốc video hiện tại; bản cũ luôn được giữ")
        _, _, sha = original_video(db, data_root, old[2])
        track = json.loads(old[5]).get("track")
        artifact = db.execute(
            """SELECT f.storage_key,f.sha256,f.byte_size FROM asset_files f
        JOIN processing_runs r ON r.run_id=f.processing_run_id WHERE f.parent_file_id=? AND f.role='OTHER'
        AND r.model_name=? AND r.status='SUCCEEDED' ORDER BY f.rowid DESC LIMIT 1""",
            (old[2], VIDEO_MODEL),
        ).fetchone()
        if artifact is None or track not in ("audio", "video"):
            raise ValueError("Thiếu kết quả đọc video để đối chiếu")
        blob = _anchor_path(data_root, "OTHER", artifact[0]).read_bytes()
        if (
            len(blob) > 256 * 1024
            or (hashlib.sha256(blob).hexdigest(), len(blob)) != artifact[1:]
        ):
            raise ValueError("Video artifact không khớp")
        pinned = json.loads(blob)
        if (
            pinned.get("format") != "CREATOR_CLOUD_VIDEO_V1"
            or pinned.get("input_sha256") != sha
            or pinned.get("input_file_id") != old[2]
        ):
            raise ValueError("Video artifact sai bản gốc")
        parsed = parse_video_result(pinned["native_result"], sha)
        duration = pinned["native_result"]["decoder"]["duration_ms"]
        if (
            not parsed
            or type(start_ms) is not int
            or type(end_ms) is not int
            or not 0 <= start_ms < end_ms <= duration
        ):
            raise ValueError("Mốc chỉnh sửa nằm ngoài video gốc")
        vid = uuid4().hex
        now = _timestamp()
        EvidenceRepository(db).append_version(
            EvidenceVersion(
                vid,
                old[0],
                old[1],
                old[3] + 1,
                old[2],
                text,
                "TIME_RANGE",
                json.dumps(
                    {"start_ms": start_ms, "end_ms": end_ms, "track": track},
                    sort_keys=True,
                ),
                "HUMAN",
                None,
                actor,
                now,
            )
        )
        db.execute(
            "INSERT INTO review_events(review_event_id,evidence_version_id,action,actor_id,reason,created_at) VALUES(?,?,'CORRECT',?,?,?)",
            (uuid4().hex, evidence_version_id, actor, reason, now),
        )
        return vid
