"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from creator_loop.backend.knowledge.evidence_correction import (
    claim_versions_needing_review,
)
from creator_loop.backend.knowledge.whole_evidence import (
    correct_whole_evidence,
    create_whole_evidence,
    read_verified_whole_source,
    reopen_whole_evidence,
)
from creator_loop.backend.runtime.database import _connect_write, open_readonly


def run_WholeEvidenceWorker(self: Any) -> None:
    try:
        db_path = self.root / "creator_loop.sqlite3"
        if self.action in ("load", "reopen", "load-correction"):
            with closing(open_readonly(db_path)) as db:
                if self.action == "load":
                    source = read_verified_whole_source(db, self.identifier, self.root)
                    result: object = (source.text_snapshot or "", source)
                else:
                    result = reopen_whole_evidence(db, self.identifier, self.root)
        else:
            with closing(_connect_write(db_path)) as db:
                if self.action == "create":
                    version = create_whole_evidence(
                        db,
                        file_id=self.identifier,
                        data_root=self.root,
                        actor=self.actor,
                        whole_source_relevant=self.confirmed,
                        content=self.content,
                    )
                    result = version.evidence_version_id
                elif self.action == "correct":
                    corrected = correct_whole_evidence(
                        db,
                        evidence_version_id=self.identifier,
                        data_root=self.root,
                        actor=self.actor,
                        reason=self.reason,
                        whole_source_relevant=self.confirmed,
                        content=self.content,
                    )
                    result = (
                        corrected.new_version_id,
                        len(claim_versions_needing_review(db, corrected.evidence_id)),
                    )
                else:
                    raise ValueError("Unknown whole-source Evidence action")
        self.result.emit(result)
    except Exception as exc:
        self.failed.emit(f"Không thể xử lý toàn nguồn: {exc}")
