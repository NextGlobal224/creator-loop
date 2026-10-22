"""Bounded cloud audio protocol; no inference, approval or budget settlement.

Initial decoder-independent intake is PCM WAV. Other codecs need a verified
cloud duration/decoder path; a file extension is never a duration bound.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import sqlite3
import unicodedata
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.knowledge.evidence import (
    Evidence,
    EvidenceRepository,
    EvidenceVersion,
)
from creator_loop.backend.knowledge.evidence_reopen import _anchor_path
from creator_loop.backend.runtime.transactions import atomic_transaction

WHISPER_MODEL = "@cf/openai/whisper-large-v3-turbo"
MAX_AUDIO_MS = 120_000
MAX_AUDIO_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CloudAudioProbe:
    sha256: str
    byte_size: int
    duration_ms: int
    sample_rate: int
    channels: int
    sample_width: int


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CloudTranscript:
    text: str
    segments: tuple[TranscriptSegment, ...]
    warnings: tuple[str, ...]
    untranscribed_ranges: tuple[tuple[int, int], ...] = ()
    review_state: str = "PENDING"


def prepare_cloud_transcription(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    file_id: str,
    run_id: str,
    version: str,
) -> dict[str, Any]:
    """Prepare one verified source run inside the durable reservation transaction.

    The caller owns Project membership, the existing included-only grant and
    idempotency. This function neither calls AI nor grants source-use permission.
    """
    if (
        not isinstance(run_id, str)
        or len(run_id) != 32
        or any(c not in "0123456789abcdef" for c in run_id)
    ):
        raise ValueError("Run identity không hợp lệ")
    with atomic_transaction(db):
        row = db.execute(
            """SELECT f.asset_id,f.storage_key,f.sha256,f.byte_size,f.duration_ms
            FROM asset_files f JOIN assets a USING(asset_id)
            WHERE f.file_id=? AND f.role='ORIGINAL' AND a.media_type='AUDIO'
            AND a.deleted_at IS NULL""",
            (file_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Cần bản ghi âm gốc còn hiệu lực")
        if db.execute(
            """SELECT 1 FROM processing_runs WHERE input_file_id=?
            AND tool_name='CLOUDFLARE_WORKERS_AI_SOURCE' AND model_name=? AND status='SUCCEEDED'""",
            (file_id, WHISPER_MODEL),
        ).fetchone():
            raise ValueError(
                "Đã có transcript; mở nguồn để nghe đối chiếu và sửa, không gọi lại AI."
            )
        raw = _anchor_path(data_root, "ORIGINAL", row[1]).read_bytes()
        probe = probe_pcm_wav(raw)
        if (probe.sha256, probe.byte_size) != row[2:4] or row[4] not in (
            None,
            probe.duration_ms,
        ):
            raise ValueError("Byte/thời lượng nguồn không khớp")
        if db.execute(
            "SELECT 1 FROM processing_runs WHERE run_id=?", (run_id,)
        ).fetchone():
            raise ValueError("Run đã tồn tại; đọc trạng thái cũ, không gửi lại")
        db.execute(
            "UPDATE asset_files SET duration_ms=? WHERE file_id=?",
            (probe.duration_ms, file_id),
        )
        db.execute(
            """INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,
            tool_name,tool_version,model_name,model_version,created_at)
            VALUES(?,?,?,'SOURCE_TRANSCRIPTION','QUEUED','CLOUDFLARE_WORKERS_AI_SOURCE',?,?,?,?)""",
            (run_id, row[0], file_id, version, WHISPER_MODEL, version, _timestamp()),
        )
        return {
            "request_id": run_id,
            "model": WHISPER_MODEL,
            "input_sha256": probe.sha256,
            "duration_ms": probe.duration_ms,
            "audio": base64.b64encode(raw).decode(),
        }


def probe_pcm_wav(raw: bytes) -> CloudAudioProbe:
    """Validate actual PCM bytes before reserving/submitting a bounded ASR job."""
    if not isinstance(raw, bytes) or not 44 <= len(raw) <= MAX_AUDIO_BYTES:
        raise ValueError("PCM WAV cần từ 44 byte đến 8 MiB; chưa gửi AI.")
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("Chỉ PCM WAV có thời lượng kiểm được trên cloud; chưa gửi AI.")
    if int.from_bytes(raw[4:8], "little") + 8 != len(raw):
        raise ValueError("Kích thước RIFF không khớp bản gốc; chưa gửi AI.")
    # Native decoders may choose different duplicate chunks; refuse ambiguity.
    offset, chunks, data_length, block_align = 12, [], 0, 0
    while offset < len(raw):
        if offset + 8 > len(raw):
            raise ValueError("RIFF chunk thiếu header; chưa gửi AI.")
        kind = raw[offset : offset + 4]
        length = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        stop = offset + 8 + length
        if stop > len(raw) or kind not in (b"fmt ", b"data", b"JUNK", b"LIST", b"bext"):
            raise ValueError("RIFF chunk chưa hỗ trợ hoặc thiếu byte; chưa gửi AI.")
        if kind == b"fmt " and (
            length != 16 or raw[offset + 8 : offset + 10] != b"\x01\x00"
        ):
            raise ValueError("WAV cần định dạng PCM đơn nghĩa; chưa gửi AI.")
        if kind == b"fmt ":
            fmt = raw[offset + 8 : stop]
            channels = int.from_bytes(fmt[2:4], "little")
            rate = int.from_bytes(fmt[4:8], "little")
            byte_rate = int.from_bytes(fmt[8:12], "little")
            block_align = int.from_bytes(fmt[12:14], "little")
            bits = int.from_bytes(fmt[14:16], "little")
            if (
                bits not in (8, 16, 24, 32)
                or channels not in (1, 2)
                or not 8000 <= rate <= 48000
                or block_align != channels * (bits // 8)
                or byte_rate != rate * block_align
            ):
                raise ValueError("PCM header mâu thuẫn; chưa gửi AI.")
        if kind == b"data":
            data_length = length
        chunks.append(kind)
        if len(chunks) > 256:
            raise ValueError("WAV vượt giới hạn 256 chunk; chưa gửi AI.")
        offset = stop + (length % 2)
    if (
        offset != len(raw)
        or chunks.count(b"fmt ") != 1
        or chunks.count(b"data") != 1
        or chunks.index(b"fmt ") > chunks.index(b"data")
    ):
        raise ValueError("WAV cần đúng một fmt/data theo thứ tự; chưa gửi AI.")
    if not block_align or data_length % block_align:
        raise ValueError("PCM data không đủ frame; chưa gửi AI.")
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            channels, width, rate, frames = (
                reader.getnchannels(),
                reader.getsampwidth(),
                reader.getframerate(),
                reader.getnframes(),
            )
            if (
                reader.getcomptype() != "NONE"
                or channels not in (1, 2)
                or width not in (1, 2, 3, 4)
                or not 8000 <= rate <= 48000
                or frames <= 0
                or frames * 1000 > MAX_AUDIO_MS * rate
            ):
                raise ValueError(
                    "PCM WAV cần 1–2 kênh, 8–48 kHz, tối đa 120 giây; chưa gửi AI."
                )
            if len(reader.readframes(frames + 1)) != frames * channels * width:
                raise ValueError("PCM WAV thiếu byte âm thanh; chưa gửi AI.")
    except (wave.Error, EOFError) as exc:
        raise ValueError("PCM WAV hỏng hoặc codec chưa hỗ trợ; chưa gửi AI.") from exc
    return CloudAudioProbe(
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        math.ceil(frames * 1000 / rate),
        rate,
        channels,
        width,
    )


def _number(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("ASR có số không hợp lệ: " + name)
    return float(value)


def _plain(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 12_000:
        raise ValueError("ASR thiếu text hoặc vượt giới hạn transcript")
    return unicodedata.normalize("NFC", value).strip()


def parse_whisper_transcript(
    result: dict[str, Any], probe: CloudAudioProbe
) -> CloudTranscript:
    """Keep uncertain passages visible; timing/text are not factual approval.

    The caller must retain the untouched native response and receipt separately.
    This parser cannot infer measured Neurons from a transcript or published rate.
    """
    if not isinstance(result, dict):
        raise ValueError("ASR response phải là object")
    text = _plain(result.get("text"))
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 500:
        raise ValueError("ASR chưa có đoạn kèm thời gian; giữ output gốc để kiểm tra.")
    info = result.get("transcription_info")
    if not isinstance(info, dict):
        raise ValueError("ASR thiếu metadata thời lượng")
    duration = _number(info.get("duration"), "duration")
    if duration <= 0 or abs(duration * 1000 - probe.duration_ms) > 1000:
        raise ValueError("Thời lượng ASR không khớp bản gốc PCM WAV")
    segments = []
    uncovered = []
    previous_start, previous_end = -1, 0
    for value in raw_segments:
        if not isinstance(value, dict):
            raise ValueError("ASR segment phải là object")
        start = math.floor(_number(value.get("start"), "start") * 1000)
        end = math.ceil(_number(value.get("end"), "end") * 1000)
        if not 0 <= start < end <= probe.duration_ms or start < previous_start:
            raise ValueError("Thời gian ASR ngoài bản gốc hoặc đảo thứ tự")
        warnings = ["OVERLAPPING_SEGMENT"] if start < previous_end else []
        if start - previous_end >= 500:
            uncovered.append((previous_end, start))
        for key, threshold, high in (
            ("avg_logprob", -1.0, False),
            ("no_speech_prob", 0.6, True),
            ("compression_ratio", 2.4, True),
        ):
            if key not in value:
                warnings.append("MISSING_" + key.upper())
                continue
            score = _number(value[key], key)
            if (
                (key == "no_speech_prob" and not 0 <= score <= 1)
                or (key == "compression_ratio" and score < 0)
                or (key == "avg_logprob" and score > 0)
            ):
                raise ValueError("ASR confidence metadata ngoài miền hợp lệ")
            if (score > threshold) if high else (score < threshold):
                warnings.append("SUSPECT_" + key.upper())
        segments.append(
            TranscriptSegment(start, end, _plain(value.get("text")), tuple(warnings))
        )
        previous_start, previous_end = start, max(previous_end, end)
    if sum(len(segment.text) for segment in segments) > 24_000:
        raise ValueError("ASR segments vượt giới hạn transcript")
    warnings = []
    if probe.duration_ms - previous_end >= 500:
        uncovered.append((previous_end, probe.duration_ms))
    if uncovered:
        warnings.append("UNTRANSCRIBED_RANGES")
    if " ".join(text.split()) != " ".join(" ".join(s.text for s in segments).split()):
        warnings.append("FULL_TEXT_SEGMENT_MISMATCH")
    return CloudTranscript(text, tuple(segments), tuple(warnings), tuple(uncovered))


def store_cloud_transcript(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    file_id: str,
    run_id: str,
    native_response: dict[str, Any],
) -> dict[str, Any]:
    """Internal model callback only; caller atomically persists returned object.

    Never expose this as a client-supplied transcript endpoint. The cloud job
    owns the native response/receipt and its outcome/usage reservation. This
    function only binds a completed response to its verified physical Original.
    """
    with atomic_transaction(db):
        row = db.execute(
            """SELECT f.asset_id,f.role,f.storage_key,f.sha256,f.byte_size,
                      f.duration_ms,a.media_type,r.status,r.model_name
               FROM asset_files f JOIN assets a USING(asset_id)
               JOIN processing_runs r ON r.asset_id=f.asset_id AND r.input_file_id=f.file_id
               WHERE f.file_id=? AND r.run_id=?
                 AND r.tool_name='CLOUDFLARE_WORKERS_AI_SOURCE'
                 AND r.task_type='SOURCE_TRANSCRIPTION' AND a.deleted_at IS NULL""",
            (file_id, run_id),
        ).fetchone()
        if (
            row is None
            or row[1] != "ORIGINAL"
            or row[6:] != ("AUDIO", "RUNNING", WHISPER_MODEL)
        ):
            raise ValueError("ASR callback không khớp nguồn/model/run đang chạy")
        raw = _anchor_path(data_root, row[1], row[2]).read_bytes()
        if len(raw) != row[4] or hashlib.sha256(raw).hexdigest() != row[3]:
            raise ValueError("Bản gốc đã thay đổi; chưa lưu transcript")
        probe = probe_pcm_wav(raw)
        if row[5] is not None and row[5] != probe.duration_ms:
            raise ValueError("Metadata thời lượng cũ không khớp byte gốc")
        transcript = parse_whisper_transcript(native_response, probe)
        ids = [uuid4().hex for _ in transcript.segments]
        artifact = {
            "format": "CREATOR_CLOUD_TRANSCRIPT_V1",
            "model": WHISPER_MODEL,
            "run_id": run_id,
            "input_file_id": file_id,
            "input_sha256": probe.sha256,
            "native_response": native_response,
            "parsed": asdict(transcript),
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
            raise ValueError("Transcript artifact vượt 256 KiB; chưa lưu phiên bản")
        sha = hashlib.sha256(blob).hexdigest()
        key, output_id, now = "storage/derived/" + sha, uuid4().hex, _timestamp()
        destination = _anchor_path(data_root, "OTHER", key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != blob:
            raise ValueError("Transcript artifact identity conflict")
        destination.write_bytes(blob)
        db.execute(
            "UPDATE asset_files SET duration_ms=? WHERE file_id=?",
            (probe.duration_ms, file_id),
        )
        db.execute(
            """INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,processing_run_id,created_at)
               VALUES(?,?,'OTHER',?,?,?,'application/json',?,?,?)""",
            (output_id, row[0], key, sha, len(blob), file_id, run_id, now),
        )
        repo = EvidenceRepository(db)
        for vid, segment in zip(ids, transcript.segments):
            eid = uuid4().hex
            repo.create_with_version(
                Evidence(eid, row[0], "SPEECH", now, None),
                EvidenceVersion(
                    vid,
                    eid,
                    row[0],
                    1,
                    file_id,
                    segment.text,
                    "TIME_RANGE",
                    json.dumps(
                        {
                            "start_ms": segment.start_ms,
                            "end_ms": segment.end_ms,
                            "track": "audio",
                        },
                        sort_keys=True,
                    ),
                    "MODEL",
                    run_id,
                    None,
                    now,
                ),
            )
        db.execute(
            "UPDATE processing_runs SET status='SUCCEEDED',finished_at=? WHERE run_id=?",
            (now, run_id),
        )
        return {
            "artifact_file_id": output_id,
            "sha256": sha,
            "storage_key": key,
            "content": blob,
            "evidence_version_ids": ids,
            "review_state": "PENDING",
        }


def correct_cloud_audio_range(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    evidence_version_id: str,
    start_ms: int,
    end_ms: int,
    content: str,
    actor: str,
    reason: str,
) -> str:
    """Append an explicit human correction against verified PCM source bytes.

    Uses the existing version/CORRECT history; never invokes native codecs,
    transcription or factual ACCEPT. Project membership is the Web caller's gate.
    """
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ValueError("Người sửa nguồn không hợp lệ")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError("Cần lý do sửa nguồn, tối đa 2000 ký tự")
    text = _plain(content)
    with atomic_transaction(db):
        row = db.execute(
            """SELECT v.evidence_id,v.asset_id,v.anchor_file_id,v.version_no,
                      v.locator_type,v.locator_data,f.storage_key,f.sha256,f.byte_size,
                      f.duration_ms,a.media_type,f.role,e.evidence_type,
                      (SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=v.evidence_id)
               FROM evidence_versions v JOIN evidences e USING(evidence_id)
               JOIN asset_files f ON f.file_id=v.anchor_file_id JOIN assets a ON a.asset_id=v.asset_id
               WHERE v.evidence_version_id=? AND e.deleted_at IS NULL AND a.deleted_at IS NULL""",
            (evidence_version_id,),
        ).fetchone()
        if (
            row is None
            or row[4] != "TIME_RANGE"
            or row[10:13] != ("AUDIO", "ORIGINAL", "SPEECH")
            or row[3] != row[13]
        ):
            raise ValueError(
                "Chỉ sửa phiên bản speech hiện tại của nguồn AUDIO còn hiệu lực"
            )
        if json.loads(row[5]).get("track") != "audio":
            raise ValueError("Nguồn speech không khớp track audio")
        raw = _anchor_path(data_root, "ORIGINAL", row[6]).read_bytes()
        probe = probe_pcm_wav(raw)
        if (
            probe.sha256 != row[7]
            or probe.byte_size != row[8]
            or probe.duration_ms != row[9]
        ):
            raise ValueError(
                "Byte hoặc thời lượng nguồn không khớp; chưa sửa transcript"
            )
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or not 0 <= start_ms < end_ms <= probe.duration_ms
        ):
            raise ValueError("Khoảng sửa nằm ngoài thời lượng nguồn")
        key, now = uuid4().hex, _timestamp()
        EvidenceRepository(db).append_version(
            EvidenceVersion(
                key,
                row[0],
                row[1],
                row[3] + 1,
                row[2],
                text,
                "TIME_RANGE",
                json.dumps(
                    {"start_ms": start_ms, "end_ms": end_ms, "track": "audio"},
                    sort_keys=True,
                ),
                "HUMAN",
                None,
                actor.strip(),
                now,
            )
        )
        db.execute(
            """INSERT INTO review_events(review_event_id,evidence_version_id,action,actor_id,reason,created_at)
               VALUES(?,?,'CORRECT',?,?,?)""",
            (uuid4().hex, evidence_version_id, actor.strip(), reason.strip(), now),
        )
        return key
