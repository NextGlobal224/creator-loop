"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from contextlib import closing
from typing import Any, cast

from creator_loop.backend.knowledge.audio_evidence import (
    create_audio_evidence,
    decode_audio_segment,
    reopen_audio_evidence,
)
from creator_loop.backend.knowledge.evidence_correction import (
    claim_versions_needing_review,
    correct_text_evidence,
)
from creator_loop.backend.knowledge.evidence_reopen import reopen_evidence_version
from creator_loop.backend.knowledge.evidence_review import (
    current_evidence_review,
    record_evidence_review,
)
from creator_loop.backend.knowledge.image_evidence import (
    create_image_evidence,
    read_verified_image,
    reopen_image_region,
)
from creator_loop.backend.knowledge.image_evidence_correction import (
    correct_image_evidence,
)
from creator_loop.backend.knowledge.text_evidence import (
    create_text_evidence,
    read_verified_text_snapshot,
)
from creator_loop.backend.knowledge.time_evidence_correction import (
    correct_time_evidence,
)
from creator_loop.backend.knowledge.video_evidence import (
    create_video_evidence,
    decode_video_frame,
    reopen_video_evidence,
)
from creator_loop.backend.library.image_thumbnail import create_image_thumbnail
from creator_loop.backend.library.media_intake import (
    intake_image_original,
    intake_video_original,
)
from creator_loop.backend.library.source_association import (
    create_source_for_asset,
    link_existing_source,
)
from creator_loop.backend.library.text_intake import intake_text_original
from creator_loop.backend.runtime.database import _connect_write, open_readonly


def run_OriginalImportWorker(self: Any) -> None:
    try:
        import_fn = {
            "TEXT": intake_text_original,
            "VIDEO": intake_video_original,
            "IMAGE": intake_image_original,
        }[self.kind]
        if self.kind == "VIDEO":
            result = intake_video_original(
                self.source, root=self.root, cancel=self.isInterruptionRequested
            )
        else:
            result = import_fn(self.source, root=self.root)
    except Exception as exc:
        self.failed.emit(f"Không thể nhập tệp: {exc}")
    else:
        self.imported.emit(result.asset_id)


def run_TextEvidenceWorker(self: Any) -> None:
    db_path = self.root / "creator_loop.sqlite3"
    try:
        if self.action == "create":
            with closing(_connect_write(db_path)) as db:
                version = create_text_evidence(
                    db,
                    file_id=self.identifier,
                    data_root=self.root,
                    start=self.range_start,
                    end=self.range_end,
                    actor=self.actor,
                )
            self.result.emit(version.evidence_version_id)
        elif self.action == "load":
            with closing(open_readonly(db_path)) as db:
                _asset_id, snapshot = read_verified_text_snapshot(
                    db, self.identifier, self.root
                )
            self.result.emit(snapshot)
        elif self.action == "load-correction":
            with closing(open_readonly(db_path)) as db:
                reopened = reopen_evidence_version(db, self.identifier, self.root)
                if reopened.locator_type != "TEXT_RANGE":
                    raise ValueError("Selected Evidence is not a text range")
                _asset_id, snapshot = read_verified_text_snapshot(
                    db, reopened.anchor_file_id, self.root
                )
                start = reopened.locator["start"]
                end = reopened.locator["end"]
                if type(start) is not int or type(end) is not int:
                    raise ValueError("Selected Evidence has an invalid text range")
            self.result.emit((snapshot, start, end))
        elif self.action == "correct":
            with closing(_connect_write(db_path)) as db:
                corrected = correct_text_evidence(
                    db,
                    evidence_version_id=self.identifier,
                    data_root=self.root,
                    start=self.range_start,
                    end=self.range_end,
                    actor=self.actor,
                    reason=self.reason,
                )
                stale_claims = claim_versions_needing_review(db, corrected.evidence_id)
            self.result.emit((corrected.new_version_id, len(stale_claims)))
        elif self.action == "reopen":
            with closing(open_readonly(db_path)) as db:
                reopened = reopen_evidence_version(db, self.identifier, self.root)
            if reopened.locator_type != "TEXT_RANGE" or reopened.text_excerpt is None:
                raise ValueError("Selected Evidence is not a text range")
            self.result.emit(reopened.text_excerpt)
        else:
            raise ValueError("Unknown Evidence action")
    except Exception as exc:
        self.failed.emit(f"Không thể xử lý Evidence: {exc}")


def run_ImageEvidenceWorker(self: Any) -> None:
    try:
        db_path = self.root / "creator_loop.sqlite3"
        if self.action == "load":
            with closing(open_readonly(db_path)) as db:
                _asset_id, image = read_verified_image(db, self.identifier, self.root)
            self.result.emit(image)
        elif self.action == "create":
            if self.region is None:
                raise ValueError("Image region is required")
            with closing(_connect_write(db_path)) as db:
                version = create_image_evidence(
                    db,
                    file_id=self.identifier,
                    data_root=self.root,
                    region=self.region,
                    content=self.content,
                    actor=self.actor,
                )
            self.result.emit(version.evidence_version_id)
        elif self.action == "reopen":
            with closing(open_readonly(db_path)) as db:
                self.result.emit(reopen_image_region(db, self.identifier, self.root))
        elif self.action == "load-correction":
            with closing(open_readonly(db_path)) as db:
                reopened = reopen_evidence_version(db, self.identifier, self.root)
                if reopened.locator_type != "IMAGE_REGION":
                    raise ValueError("Selected Evidence is not an image region")
                _asset_id, image = read_verified_image(
                    db, reopened.anchor_file_id, self.root
                )
                row = db.execute(
                    "SELECT content FROM evidence_versions WHERE evidence_version_id=?",
                    (self.identifier,),
                ).fetchone()
                if row is None:
                    raise ValueError("Selected Evidence version does not exist")
            self.result.emit(
                (image, cast(dict[str, float], reopened.locator), str(row[0]))
            )
        elif self.action == "correct":
            if self.region is None:
                raise ValueError("Image region is required")
            with closing(_connect_write(db_path)) as db:
                corrected = correct_image_evidence(
                    db,
                    evidence_version_id=self.identifier,
                    data_root=self.root,
                    region=self.region,
                    content=self.content,
                    actor=self.actor,
                    reason=self.reason,
                )
                stale_claims = claim_versions_needing_review(db, corrected.evidence_id)
            self.result.emit((corrected.new_version_id, len(stale_claims)))
        else:
            raise ValueError("Unknown image Evidence action")
    except Exception as exc:
        self.failed.emit(f"Không thể xử lý Evidence ảnh: {exc}")


def run_ThumbnailWorker(self: Any) -> None:
    try:
        result = create_image_thumbnail(self.file_id, data_root=self.root)
        self.completed.emit(result.run_id)
    except Exception as exc:
        self.failed.emit(f"Không thể tạo thumbnail: {exc}")


def run_VideoEvidenceWorker(self: Any) -> None:
    db_path = self.root / "creator_loop.sqlite3"
    try:
        if self.action == "load":
            with closing(open_readonly(db_path)) as db:
                _asset_id, decoded = decode_video_frame(
                    db, self.identifier, self.root, start_ms=0
                )
            self.result.emit(decoded)
        elif self.action == "create":
            with closing(_connect_write(db_path)) as db:
                version = create_video_evidence(
                    db,
                    file_id=self.identifier,
                    data_root=self.root,
                    start_ms=self.start_ms,
                    end_ms=self.end_ms,
                    content=self.content,
                    actor=self.actor,
                )
            self.result.emit(version.evidence_version_id)
        elif self.action == "correct":
            with closing(_connect_write(db_path)) as db:
                corrected = correct_time_evidence(
                    db,
                    evidence_version_id=self.identifier,
                    data_root=self.root,
                    start_ms=self.start_ms,
                    end_ms=self.end_ms,
                    content=self.content,
                    actor=self.actor,
                    reason=self.reason,
                )
                stale_count = len(
                    claim_versions_needing_review(db, corrected.evidence_id)
                )
            self.result.emit((corrected.new_version_id, stale_count))
        elif self.action in ("reopen", "load-correction"):
            with closing(open_readonly(db_path)) as db:
                self.result.emit(reopen_video_evidence(db, self.identifier, self.root))
        else:
            raise ValueError("Unknown video Evidence action")
    except Exception as exc:
        self.failed.emit(f"Không thể xử lý Evidence video: {exc}")


def run_AudioEvidenceWorker(self: Any) -> None:
    db_path = self.root / "creator_loop.sqlite3"
    try:
        if self.action == "load":
            with closing(open_readonly(db_path)) as db:
                _asset_id, decoded = decode_audio_segment(
                    db, self.identifier, self.root, start_ms=0, end_ms=1
                )
            self.result.emit(decoded)
        elif self.action == "create":
            with closing(_connect_write(db_path)) as db:
                version = create_audio_evidence(
                    db,
                    file_id=self.identifier,
                    data_root=self.root,
                    start_ms=self.start_ms,
                    end_ms=self.end_ms,
                    content=self.content,
                    actor=self.actor,
                    evidence_type=self.evidence_type,
                )
            self.result.emit(version.evidence_version_id)
        elif self.action == "correct":
            with closing(_connect_write(db_path)) as db:
                corrected = correct_time_evidence(
                    db,
                    evidence_version_id=self.identifier,
                    data_root=self.root,
                    start_ms=self.start_ms,
                    end_ms=self.end_ms,
                    content=self.content,
                    actor=self.actor,
                    reason=self.reason,
                )
                stale_count = len(
                    claim_versions_needing_review(db, corrected.evidence_id)
                )
            self.result.emit((corrected.new_version_id, stale_count))
        elif self.action == "load-correction":
            with closing(open_readonly(db_path)) as db:
                reopened = reopen_audio_evidence(db, self.identifier, self.root)
                kind = db.execute(
                    "SELECT e.evidence_type FROM evidences e\n                           JOIN evidence_versions v ON v.evidence_id=e.evidence_id\n                           WHERE v.evidence_version_id=?",
                    (self.identifier,),
                ).fetchone()[0]
                if kind not in ("SPEECH", "OTHER"):
                    raise ValueError(
                        "Audio correction requires SPEECH or OTHER Evidence"
                    )
            self.result.emit((*reopened, str(kind)))
        elif self.action == "reopen":
            with closing(open_readonly(db_path)) as db:
                self.result.emit(reopen_audio_evidence(db, self.identifier, self.root))
        else:
            raise ValueError("Unknown audio Evidence action")
    except Exception as exc:
        self.failed.emit(f"Không thể xử lý Evidence audio: {exc}")


def run_EvidenceReviewWorker(self: Any) -> None:
    try:
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            record_evidence_review(
                db,
                evidence_version_id=self.version_id,
                data_root=self.root,
                action=self.action,
                actor=self.actor,
                reason=self.reason,
            )
            state = current_evidence_review(db, self.version_id)
        self.completed.emit(state)
    except Exception as exc:
        self.failed.emit(f"Không thể review Evidence: {exc}")


def run_SourceWorker(self: Any) -> None:
    try:
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            if self.existing_source_id is None:
                source_id = create_source_for_asset(
                    db,
                    asset_id=self.asset_id,
                    details=self.details,
                    relationship_type=self.relationship_type,
                )
            else:
                source_id = self.existing_source_id
                link_existing_source(
                    db,
                    source_id=source_id,
                    asset_id=self.asset_id,
                    relationship_type=self.relationship_type,
                )
        self.linked.emit(source_id)
    except Exception as exc:
        self.failed.emit(f"Không thể gắn Source: {exc}")
