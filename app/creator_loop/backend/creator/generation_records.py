"""Shared generation persistence and display; no native process ownership."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from creator_loop.backend.knowledge.claims import EvidenceLink, _timestamp, create_claim
from creator_loop.backend.runtime.database import _connect_write
from creator_loop.backend.runtime.transactions import atomic_transaction
from creator_loop.shared.workflow import CreatorRequest, ResearchMaterial

if TYPE_CHECKING:
    from creator_loop.backend.analysis.codex_creator import CodexCreator
    from creator_loop.backend.analysis.creator_llm import LocalCreatorLLM
    from creator_loop.backend.analysis.workers_creator import WorkersCreator
    from creator_loop.backend.creator.research_pipeline import ResearchPipeline

PREFIX = "CREATOR_LLM_V1\n"


def _persist(
    pipeline: ResearchPipeline,
    request: CreatorRequest,
    materials: list[ResearchMaterial],
    result: dict[str, Any],
    provider: LocalCreatorLLM | CodexCreator | WorkersCreator,
    fingerprint: str,
    run_id: str,
) -> str:
    wid, now = uuid4().hex, _timestamp()
    with (
        closing(_connect_write(pipeline.root / "creator_loop.sqlite3")) as db,
        atomic_transaction(db),
    ):
        if (
            db.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND status='ACTIVE' AND deleted_at IS NULL",
                (request.project_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("Project changed while generating")
        prepared: list[ResearchMaterial] = []
        for material in materials:
            if pipeline._verify(db, material) != material:
                raise ValueError("Material changed while generating")
            if (
                material.media_type == "TEXT"
                and material.file_id
                and not material.evidence_version_ids
            ):
                material = replace(
                    material,
                    evidence_version_ids=(pipeline._text_evidence(db, material),),
                )
            if not material.evidence_version_ids:
                raise ValueError(
                    "LLM inputs require reopenable exact Evidence Versions"
                )
            prepared.append(material)
        refs = {m.material_id: m.evidence_version_ids for m in prepared}
        for claim in result["factual_claims"]:
            support = []
            for mid in claim["material_ids"]:
                matched = [
                    ev
                    for ev in refs[mid]
                    if claim["source_quote"]
                    in db.execute(
                        "SELECT content FROM evidence_versions WHERE evidence_version_id=?",
                        (ev,),
                    ).fetchone()[0]
                ]
                if not matched:
                    raise ValueError(
                        "Claim quote is not in cited exact Evidence content"
                    )
                support.extend(matched)
            saved = create_claim(
                db,
                claim_type="FACTUAL",
                statement=claim["text"],
                actor=provider.name,
                links=[EvidenceLink(ev, "SUPPORTS") for ev in dict.fromkeys(support)],
            )
            claim["claim_version_id"] = saved.claim_version_id
        editorial = create_claim(
            db,
            claim_type="EDITORIAL_HYPOTHESIS",
            statement="Giả thuyết biên tập, chưa xác nhận hiệu quả: "
            + result["direction"],
            actor=provider.name,
            links=[
                EvidenceLink(ev, "CONTEXT")
                for ev in dict.fromkeys(
                    ev for m in prepared for ev in m.evidence_version_ids
                )
            ],
        )
        db.execute(
            "INSERT INTO creator_workflows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wid,
                request.project_id,
                request.mode,
                request.output_format,
                request.purpose.strip(),
                request.audience.strip(),
                request.style.strip(),
                provider.name,
                provider.version,
                fingerprint,
                PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True),
                request.direction.strip() or result["direction"],
                now,
            ),
        )
        position = 0
        for material in prepared:
            for ev in material.evidence_version_ids:
                fid, digest = db.execute(
                    "SELECT v.anchor_file_id,f.sha256 FROM evidence_versions v JOIN asset_files f ON f.file_id=v.anchor_file_id WHERE v.evidence_version_id=?",
                    (ev,),
                ).fetchone()
                db.execute(
                    "INSERT INTO creator_workflow_materials VALUES(?,?,?,?,?,?)",
                    (wid, position, material.source_id, fid, digest, ev),
                )
                position += 1
        for cid in [
            editorial.claim_version_id,
            *(c["claim_version_id"] for c in result["factual_claims"]),
        ]:
            db.execute("INSERT INTO creator_workflow_claims VALUES(?,?)", (wid, cid))
        db.execute(
            "UPDATE processing_runs SET status='SUCCEEDED',finished_at=? WHERE run_id=? AND status='RUNNING'",
            (_timestamp(), run_id),
        )
    return wid


def decode_analysis(text: str) -> dict[str, Any] | None:
    return json.loads(text[len(PREFIX) :]) if text.startswith(PREFIX) else None


def display_analysis(value: dict[str, Any]) -> str:
    provenance = value["provenance"]
    lines = [
        f"Backend {provenance['backend']}/{provenance['version']}",
        "Quan sát từ exact input:",
    ]
    lines.extend(
        f"• {o['material_id']}: {o['observation']}\n  Trích nguyên văn: {o['quote']}"
        for o in value["observations"]
    )
    lines.append("Suy luận/giả thuyết biên tập (chưa xác nhận):")
    lines.extend("• " + s for s in value["inferences"])
    lines.extend(
        [
            "Hướng: " + value["direction"],
            "Số liệu tương tác/nguyên nhân viral: Chưa rõ nếu input không có căn cứ.",
            "Claim và Evidence chưa được người dùng ACCEPT; duyệt đăng là gate riêng.",
        ]
    )
    return "\n".join(lines)
