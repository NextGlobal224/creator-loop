"""V1 factual review gate for the exact versions pinned by a sealed Package.

This gate does not grant Approval or media rights. Callers must evaluate it again
inside the publication transaction; a previous successful inspection is no permit.
"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from creator_loop.claim_review import current_claim_review
from creator_loop.evidence_reopen import EvidenceReopenError, reopen_evidence_version
from creator_loop.evidence_review import current_evidence_review
from creator_loop.publication_media import hold_registered_file


@dataclass(frozen=True)
class PublicationBlocker:
    code: str
    version_id: str
    message: str


class FactualPublicationBlocked(ValueError):
    def __init__(self, blockers: tuple[PublicationBlocker, ...]) -> None:
        self.blockers = blockers
        super().__init__("\n".join(blocker.message for blocker in blockers))


def factual_review_blockers(
    db: sqlite3.Connection,
    *,
    package_id: str,
    data_root: Path,
    handles: ExitStack | None = None,
) -> tuple[PublicationBlocker, ...]:
    """Inspect every FACTUAL citation, never substituting a newer version.

    The caller owns the read/write transaction to keep the DB snapshot consistent.
    Accepted SUPPORTS must still have a live, verifiable physical anchor. One
    valid accepted support is sufficient; CONTEXT and CONTRADICTS never count.
    """
    package = db.execute(
        """SELECT p.draft_version_id FROM publication_packages p
        JOIN draft_versions v ON v.draft_version_id=p.draft_version_id
        JOIN drafts d ON d.draft_id=v.draft_id AND d.project_id=p.project_id
        JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id
        WHERE p.package_id=? AND p.sealed_at IS NOT NULL AND p.item_count>0
        AND p.item_count=(SELECT count(*) FROM publication_package_items i
                         WHERE i.package_id=p.package_id)""",
        (package_id,),
    ).fetchone()
    if package is None:
        raise ValueError("Sealed complete Package with a sealed Draft is required")
    blockers: list[PublicationBlocker] = []
    claims = db.execute(
        """SELECT v.claim_version_id,c.deleted_at,s.sealed_at
        FROM draft_claims dc JOIN claim_versions v ON v.claim_version_id=dc.claim_version_id
        JOIN claims c ON c.claim_id=v.claim_id
        LEFT JOIN claim_version_seals s ON s.claim_version_id=v.claim_version_id
        WHERE dc.draft_version_id=? AND c.claim_type='FACTUAL'
        ORDER BY v.claim_version_id""",
        (package[0],),
    ).fetchall()
    for version_id, deleted_at, sealed_at in claims:
        if deleted_at is not None or sealed_at is None:
            blockers.append(
                PublicationBlocker(
                    "claim_unavailable",
                    version_id,
                    f"Claim Version {version_id}: cần bản sealed còn hiệu lực.",
                )
            )
        review = current_claim_review(db, version_id)
        if review != "ACCEPT":
            blockers.append(
                PublicationBlocker(
                    "claim_not_accepted",
                    version_id,
                    f"Claim Version {version_id}: cần người duyệt ACCEPT; hiện tại {review}.",
                )
            )
        supports = db.execute(
            """SELECT v.evidence_version_id,
            CASE WHEN e.deleted_at IS NOT NULL OR a.deleted_at IS NOT NULL THEN 1 ELSE NULL END
            FROM claim_evidence ce JOIN evidence_versions v
              ON v.evidence_version_id=ce.evidence_version_id
            JOIN evidences e ON e.evidence_id=v.evidence_id
            JOIN assets a ON a.asset_id=v.asset_id
            WHERE ce.claim_version_id=? AND ce.relation_type='SUPPORTS'
            ORDER BY v.evidence_version_id""",
            (version_id,),
        ).fetchall()
        accepted = 0
        failures: list[str] = []
        for evidence_id, deleted in supports:
            state = current_evidence_review(db, evidence_id)
            if deleted is not None or state != "ACCEPT":
                failures.append(f"{evidence_id}: {'deleted' if deleted else state}")
                continue
            try:
                reopened = reopen_evidence_version(db, evidence_id, data_root)
                if handles is not None:
                    digest = db.execute(
                        "SELECT sha256 FROM asset_files WHERE file_id=?",
                        (reopened.anchor_file_id,),
                    ).fetchone()[0]
                    hold_registered_file(
                        db,
                        file_id=reopened.anchor_file_id,
                        expected_digest=digest,
                        data_root=data_root,
                        handles=handles,
                    )
            except EvidenceReopenError as exc:
                failures.append(f"{evidence_id}: {exc.reason}")
            except ValueError as exc:
                failures.append(f"{evidence_id}: {exc}")
            else:
                accepted += 1
        if accepted < 1:
            details = "; ".join(failures) or "không có liên kết SUPPORTS"
            blockers.append(
                PublicationBlocker(
                    "supports_not_accepted",
                    version_id,
                    f"Claim Version {version_id}: cần ít nhất 1 Evidence Version SUPPORTS "
                    f"được người duyệt ACCEPT và mở lại được; đạt {accepted}/1 ({details}).",
                )
            )
    return tuple(blockers)


def require_factual_reviews(
    db: sqlite3.Connection,
    *,
    package_id: str,
    data_root: Path,
    handles: ExitStack | None = None,
) -> None:
    blockers = factual_review_blockers(
        db, package_id=package_id, data_root=data_root, handles=handles
    )
    if blockers:
        raise FactualPublicationBlocked(blockers)
