"""Original-bound cloud image observations; no model call or human approval here."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
import struct
import unicodedata
import zlib
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
from creator_loop.backend.knowledge.locator import validate_locator
from creator_loop.backend.runtime.transactions import atomic_transaction

IMAGE_MODEL = "@cf/google/gemma-4-26b-a4b-it"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
WHOLE_IMAGE = {"x": 0, "y": 0, "width": 1, "height": 1}


@dataclass(frozen=True)
class CloudImageProbe:
    sha256: str
    byte_size: int
    mime_type: str
    encoded_width: int
    encoded_height: int


@dataclass(frozen=True)
class CloudImageObservation:
    text: str
    description: str
    visible_text: str
    uncertain: tuple[str, ...]
    unread: tuple[str, ...]
    review_state: str = "PENDING"


def probe_image(raw: bytes) -> CloudImageProbe:
    """Bound the encoded source without a Windows/native decoder or conversion.

    This is a header/size probe, not a decoded-pixel or content quality claim.
    The fixed cloud vision model receives these exact original bytes.
    """
    if not isinstance(raw, bytes) or not 24 <= len(raw) <= MAX_IMAGE_BYTES:
        raise ValueError("Ảnh cần từ 24 byte đến 8 MiB; chưa gửi AI.")
    width = height = 0
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(raw) < 45 or raw[8:16] != b"\0\0\0\rIHDR":
            raise ValueError("PNG thiếu header gốc hợp lệ; chưa gửi AI.")
        if zlib.crc32(raw[12:29]) != int.from_bytes(raw[29:33], "big"):
            raise ValueError("PNG header không khớp CRC; chưa gửi AI.")
        width, height = struct.unpack(">II", raw[16:24])
        offset = 8
        while offset + 12 <= len(raw):
            length = int.from_bytes(raw[offset : offset + 4], "big")
            kind = raw[offset + 4 : offset + 8]
            if offset + length + 12 > len(raw):
                raise ValueError("PNG chunk thiếu byte; chưa gửi AI.")
            if kind == b"acTL":
                raise ValueError(
                    "Ảnh động cần luồng đọc nhiều khung; chưa gửi AI hoặc suy rằng đã đọc mọi khung."
                )
            offset += length + 12
            if kind == b"IEND":
                if length != 0 or offset != len(raw):
                    raise ValueError("PNG kết thúc không đơn nghĩa; chưa gửi AI.")
                break
        else:
            raise ValueError("PNG thiếu IEND; chưa gửi AI.")
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9"):
        offset = 2
        while offset + 4 <= len(raw):
            if raw[offset] != 255:
                raise ValueError("JPEG marker không hợp lệ; chưa gửi AI.")
            while offset < len(raw) and raw[offset] == 255:
                offset += 1
            marker = raw[offset]
            offset += 1
            if marker in (0xDA, 0xD9):
                if raw.find(b"\xff\xd9", offset) != len(raw) - 2:
                    raise ValueError(
                        "JPEG có phần sau khung đầu chưa đọc; chưa gửi AI."
                    )
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            length = int.from_bytes(raw[offset : offset + 2], "big")
            if length < 2 or offset + length > len(raw):
                raise ValueError("JPEG segment thiếu byte; chưa gửi AI.")
            if marker in (0xC0, 0xC1, 0xC2):
                if width or length < 8:
                    raise ValueError("JPEG frame không đơn nghĩa; chưa gửi AI.")
                height, width = struct.unpack(">HH", raw[offset + 3 : offset + 7])
            offset += length
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        if int.from_bytes(raw[4:8], "little") + 8 != len(raw):
            raise ValueError("WebP kích thước gốc không khớp; chưa gửi AI.")
        offset = 12
        frame_seen = False
        while offset + 8 <= len(raw):
            kind = raw[offset : offset + 4]
            length = int.from_bytes(raw[offset + 4 : offset + 8], "little")
            payload = raw[offset + 8 : offset + 8 + length]
            if len(payload) != length:
                raise ValueError("WebP chunk thiếu byte; chưa gửi AI.")
            if (
                kind in (b"ANIM", b"ANMF")
                or kind == b"VP8X"
                and payload
                and payload[0] & 2
            ):
                raise ValueError("Ảnh động cần luồng đọc nhiều khung; chưa gửi AI.")
            if kind == b"VP8X" and length == 10:
                if width or frame_seen:
                    raise ValueError("WebP canvas không đơn nghĩa; chưa gửi AI.")
                width = 1 + int.from_bytes(payload[4:7], "little")
                height = 1 + int.from_bytes(payload[7:10], "little")
            elif kind == b"VP8 " and length >= 10 and payload[3:6] == b"\x9d\x01\x2a":
                w, h = struct.unpack("<HH", payload[6:10])
                w, h = w & 0x3FFF, h & 0x3FFF
                if frame_seen or width and (width, height) != (w, h):
                    raise ValueError("WebP frame/canvas không khớp; chưa gửi AI.")
                frame_seen = True
                if not width:
                    width, height = w, h
            elif kind == b"VP8L" and length >= 5 and payload[0] == 0x2F:
                value = int.from_bytes(payload[1:5], "little")
                w, h = (value & 0x3FFF) + 1, ((value >> 14) & 0x3FFF) + 1
                if frame_seen or width and (width, height) != (w, h):
                    raise ValueError("WebP frame/canvas không khớp; chưa gửi AI.")
                frame_seen = True
                if not width:
                    width, height = w, h
            offset += 8 + length + (length & 1)
        if offset != len(raw) or not frame_seen:
            raise ValueError("WebP phần cuối thiếu byte; chưa gửi AI.")
        mime = "image/webp"
    else:
        raise ValueError(
            "Đọc ảnh hỗ trợ PNG, JPEG và WebP tĩnh; giữ bản gốc khác để xử lý phù hợp."
        )
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("Ảnh thiếu kích thước hoặc vượt 80 triệu pixel; chưa gửi AI.")
    return CloudImageProbe(
        hashlib.sha256(raw).hexdigest(), len(raw), mime, width, height
    )


def _original(
    db: sqlite3.Connection, root: Path, file_id: str
) -> tuple[str, bytes, CloudImageProbe]:
    row = db.execute(
        """SELECT f.asset_id,f.storage_key,f.sha256,f.byte_size,f.mime_type
        FROM asset_files f JOIN assets a USING(asset_id)
        WHERE f.file_id=? AND f.role='ORIGINAL' AND a.media_type='IMAGE' AND a.deleted_at IS NULL""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Cần ảnh gốc còn hiệu lực")
    raw = _anchor_path(root, "ORIGINAL", row[1]).read_bytes()
    probe = probe_image(raw)
    if (probe.sha256, probe.byte_size, probe.mime_type) != row[2:5]:
        raise ValueError("Byte/loại ảnh gốc không khớp")
    return row[0], raw, probe


def prepare_cloud_image(
    db: sqlite3.Connection, *, data_root: Path, file_id: str, run_id: str, version: str
) -> dict[str, Any]:
    if (
        not re.fullmatch("[a-f0-9]{32}", run_id)
        or not isinstance(version, str)
        or not version.strip()
    ):
        raise ValueError("Run/version đọc ảnh không hợp lệ")
    with atomic_transaction(db):
        asset_id, raw, probe = _original(db, data_root, file_id)
        if db.execute(
            "SELECT 1 FROM processing_runs WHERE input_file_id=? AND tool_name='CLOUDFLARE_WORKERS_AI_SOURCE' AND model_name=? AND status='SUCCEEDED'",
            (file_id, IMAGE_MODEL),
        ).fetchone():
            raise ValueError(
                "Đã có phân tích ảnh; mở nguồn để đối chiếu/sửa, không gọi lại AI."
            )
        db.execute(
            """INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,model_name,model_version,created_at)
            VALUES(?,?,?,'SOURCE_IMAGE_ANALYSIS','QUEUED','CLOUDFLARE_WORKERS_AI_SOURCE',?,?,?,?)""",
            (run_id, asset_id, file_id, version, IMAGE_MODEL, version, _timestamp()),
        )
        return {
            "request_id": run_id,
            "model": IMAGE_MODEL,
            "input_sha256": probe.sha256,
            "mime_type": probe.mime_type,
            "image": base64.b64encode(raw).decode(),
        }


def _plain(value: Any, limit: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ValueError("Nội dung phân tích ảnh không hợp lệ")
    value = unicodedata.normalize("NFC", value).strip()
    if not value and not empty:
        raise ValueError("Phân tích ảnh thiếu nội dung")
    return value


def parse_image_observation(native: Any) -> CloudImageObservation:
    if not isinstance(native, dict) or len(native.get("choices", [])) != 1:
        raise ValueError("Vision response không đúng giao thức")
    choice = native["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Phân tích ảnh chưa kết thúc đầy đủ; giữ output gốc")
    message = choice.get("message", {})
    raw = message.get("content")
    if (
        message.get("refusal")
        or not isinstance(raw, str)
        or len(raw.encode()) > 128 * 1024
    ):
        raise ValueError("Vision response từ chối/thiếu/vượt giới hạn")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("Vision JSON có trường trùng lặp")
        return value

    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != {
        "description",
        "visible_text",
        "uncertain",
        "unread",
    }:
        raise ValueError(
            "Phân tích ảnh thiếu trường; không sửa output để biến thành PASS"
        )
    description = _plain(value["description"], 8000)
    visible = _plain(value["visible_text"], 12000, empty=True)
    notes = []
    for field in ("uncertain", "unread"):
        if not isinstance(value[field], list) or len(value[field]) > 32:
            raise ValueError("Phân tích ảnh thiếu phạm vi chưa chắc/chưa đọc")
        notes.append(tuple(_plain(item, 1000) for item in value[field]))
    return CloudImageObservation(
        description + ("\nChữ đọc được:\n" + visible if visible else ""),
        description,
        visible,
        notes[0],
        notes[1],
    )


def store_cloud_image(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    file_id: str,
    run_id: str,
    native_response: Any,
) -> dict[str, Any]:
    with atomic_transaction(db):
        asset_id, _, probe = _original(db, data_root, file_id)
        row = db.execute(
            "SELECT status,model_name FROM processing_runs WHERE run_id=? AND asset_id=? AND input_file_id=? AND task_type='SOURCE_IMAGE_ANALYSIS' AND tool_name='CLOUDFLARE_WORKERS_AI_SOURCE'",
            (run_id, asset_id, file_id),
        ).fetchone()
        if row != ("RUNNING", IMAGE_MODEL):
            raise ValueError("Vision callback không khớp ảnh/model/run đang chạy")
        parsed = parse_image_observation(native_response)
        contents = [("VISUAL_OBSERVATION", parsed.description)]
        if parsed.visible_text:
            contents.append(("OCR_TEXT", parsed.visible_text))
        ids = [uuid4().hex for _ in contents]
        artifact = {
            "format": "CREATOR_CLOUD_IMAGE_V1",
            "model": IMAGE_MODEL,
            "run_id": run_id,
            "input_file_id": file_id,
            "input_sha256": probe.sha256,
            "native_response": native_response,
            "parsed": asdict(parsed),
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
            raise ValueError("Phân tích ảnh vượt 256 KiB; chưa lưu phiên bản")
        sha = hashlib.sha256(blob).hexdigest()
        key, output_id, now = "storage/derived/" + sha, uuid4().hex, _timestamp()
        destination = _anchor_path(data_root, "OTHER", key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != blob:
            raise ValueError("Image artifact identity conflict")
        destination.write_bytes(blob)
        db.execute(
            """INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,parent_file_id,processing_run_id,created_at)
            VALUES(?,?,'OTHER',?,?,?,'application/json',?,?,?)""",
            (output_id, asset_id, key, sha, len(blob), file_id, run_id, now),
        )
        repo = EvidenceRepository(db)
        for vid, (kind, content) in zip(ids, contents):
            eid = uuid4().hex
            repo.create_with_version(
                Evidence(eid, asset_id, kind, now, None),
                EvidenceVersion(
                    vid,
                    eid,
                    asset_id,
                    1,
                    file_id,
                    content,
                    "IMAGE_REGION",
                    json.dumps(WHOLE_IMAGE, sort_keys=True),
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


def correct_cloud_image(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    evidence_version_id: str,
    region: dict[str, float],
    content: str,
    actor: str,
    reason: str,
) -> str:
    text, actor, reason = (
        _plain(content, 20000),
        _plain(actor, 200),
        _plain(reason, 2000),
    )
    locator = json.dumps(region, sort_keys=True, allow_nan=False)
    validate_locator("IMAGE_REGION", locator)
    if any(not math.isfinite(v) for v in region.values()):
        raise ValueError("Vùng ảnh không hữu hạn")
    with atomic_transaction(db):
        row = db.execute(
            """SELECT v.evidence_id,v.asset_id,v.anchor_file_id,v.version_no,v.locator_type,e.evidence_type,
            (SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=v.evidence_id)
            FROM evidence_versions v JOIN evidences e USING(evidence_id) WHERE v.evidence_version_id=? AND e.deleted_at IS NULL""",
            (evidence_version_id,),
        ).fetchone()
        if (
            row is None
            or row[4] != "IMAGE_REGION"
            or row[5] not in ("VISUAL_OBSERVATION", "OCR_TEXT")
            or row[3] != row[6]
        ):
            raise ValueError("Chỉ sửa phiên bản vùng ảnh hiện tại")
        asset_id, _, _ = _original(db, data_root, row[2])
        if asset_id != row[1]:
            raise ValueError("Vùng ảnh không cùng Asset")
        vid, now = uuid4().hex, _timestamp()
        EvidenceRepository(db).append_version(
            EvidenceVersion(
                vid,
                row[0],
                asset_id,
                row[3] + 1,
                row[2],
                text,
                "IMAGE_REGION",
                locator,
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
