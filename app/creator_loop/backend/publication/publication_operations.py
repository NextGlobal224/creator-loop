"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
from typing import Any, cast

from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.knowledge.whole_evidence import (
    WholeSource,
    read_verified_whole_source,
)
from creator_loop.backend.publication.packages import PackageItem, PublicationRepository
from creator_loop.backend.publication.publication import (
    ApprovalDecision,
    Post,
)
from creator_loop.backend.publication.publication_media import hold_registered_file
from creator_loop.backend.runtime.database import open_readonly


@dataclass(frozen=True)
class PublicationTask:
    action: str
    package_id: str = ""
    fingerprint: str = ""
    project_id: str = ""
    draft_version_id: str = ""
    platform: str = ""
    format: str = ""
    items: tuple[PackageItem, ...] = ()
    actor: str = ""
    reason: str = ""
    post_id: str = ""
    external_id: str | None = None
    external_url: str | None = None
    published_at: str = ""
    file_id: str = ""
    digest: str = ""
    text_to_copy: str = ""


@dataclass(frozen=True)
class PackageMediaPreview:
    source: WholeSource
    handles: ExitStack


def run_PublicationWorker(self: Any) -> None:
    try:
        task = self.task
        repo = PublicationRepository(self.root / "creator_loop.sqlite3")
        result: object
        if task.action == "CREATE":
            repo.create_package(
                package_id=task.package_id,
                project_id=task.project_id,
                draft_version_id=task.draft_version_id,
                platform=task.platform,
                format=task.format,
                created_at=_timestamp(),
                items=list(task.items),
                data_root=self.root,
            )
            result = task.package_id
        elif task.action == "CHECK":
            result = repo.publication_blockers(
                package_id=task.package_id, data_root=self.root
            )
        elif task.action in ("APPROVED", "REJECTED", "REVOKED"):
            result = repo.record_approval(
                package_id=task.package_id,
                expected_fingerprint=task.fingerprint,
                decision=cast(ApprovalDecision, task.action),
                actor=task.actor,
                reason=task.reason,
                data_root=self.root,
            )
        elif task.action in ("PREPARE", "COPY_TEXT"):
            result = repo.prepare_manual_post(
                post_id=task.post_id,
                package_id=task.package_id,
                expected_fingerprint=task.fingerprint,
                data_root=self.root,
            )
            if task.action == "COPY_TEXT" and cast(Post, result).status != "PENDING":
                raise ValueError("Copy requires this Post to remain PENDING")
        elif task.action == "PUBLISHED":
            result = repo.record_manual_post(
                post_id=task.post_id,
                package_id=task.package_id,
                expected_fingerprint=task.fingerprint,
                external_post_id=task.external_id,
                external_url=task.external_url,
                published_at=task.published_at,
                data_root=self.root,
            )
        elif task.action == "PREVIEW":
            with closing(open_readonly(repo.db_path)) as db, ExitStack() as handles:
                db.execute("BEGIN")
                hold_registered_file(
                    db,
                    file_id=task.file_id,
                    expected_digest=task.digest,
                    data_root=self.root,
                    handles=handles,
                )
                source = read_verified_whole_source(db, task.file_id, self.root)
                result = PackageMediaPreview(source, handles.pop_all())
        else:
            raise ValueError("Unknown Publication operation")
        self.completed.emit(result)
    except Exception as exc:
        self.failed.emit(str(exc))
