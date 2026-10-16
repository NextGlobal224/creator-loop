"""Local LLM workflow persistence in the existing canonical SQLite tables."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from creator_loop.backend.analysis.codex_creator import CodexCreator
from creator_loop.backend.analysis.creator_llm import LocalCreatorLLM
from creator_loop.backend.analysis.provider_selection import selected_provider
from creator_loop.backend.analysis.workers_analysis_reuse import (
    receipt_fingerprint as _analysis_receipt_fingerprint,
)
from creator_loop.backend.analysis.workers_creator import WorkersCreator
from creator_loop.backend.knowledge.claims import EvidenceLink, _timestamp, create_claim
from creator_loop.backend.runtime.database import _connect_write, open_readonly
from creator_loop.backend.runtime.runtime_ownership import create_workspace_marker
from creator_loop.backend.runtime.transactions import atomic_transaction
from creator_loop.shared.workflow import CreatorRequest, ResearchMaterial

if TYPE_CHECKING:
    from creator_loop.backend.creator.research_pipeline import ResearchPipeline

PREFIX = "CREATOR_LLM_V1\n"


def analyze_local(
    pipeline: ResearchPipeline, request: CreatorRequest, cancelled: Callable[[], bool]
) -> str:
    provider = selected_provider(pipeline.root, pipeline.provider_override)
    from creator_loop.backend.analysis.creator_llm import response_schema
    from creator_loop.backend.analysis.generation_prompt import generation_prompt

    available = {m.material_id: m for m in pipeline.materials()}
    if any(mid not in available for mid in request.material_ids):
        raise ValueError("Selected saved material not found")
    with closing(open_readonly(pipeline.root / "creator_loop.sqlite3")) as db:
        if (
            db.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND status='ACTIVE' AND deleted_at IS NULL",
                (request.project_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("Active Project required")
        materials = [
            pipeline._verify(db, available[mid]) for mid in request.material_ids
        ]
        analysis_receipt_hash = (
            _analysis_receipt_fingerprint(
                pipeline.root, pipeline.workers_analysis_receipt
            )
            if isinstance(provider, WorkersCreator)
            and pipeline.workers_analysis_receipt is not None
            else None
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "request": asdict(request),
                    "materials": [asdict(m) for m in materials],
                    "backend": (provider.name, provider.version),
                    "generation_prompt": generation_prompt(request, materials),
                    "writing_prompt": generation_prompt(
                        request, materials, only_draft=True
                    ),
                    "response_schema": response_schema(
                        [m.material_id for m in materials]
                    ),
                    **(
                        {"analysis_receipt_sha256": analysis_receipt_hash}
                        if isinstance(provider, WorkersCreator)
                        and pipeline.workers_analysis_receipt is not None
                        else {}
                    ),
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        cached = db.execute(
            "SELECT workflow_id FROM creator_workflows WHERE input_fingerprint=? AND backend_name=? AND backend_version=? ORDER BY rowid DESC LIMIT 1",
            (fingerprint, provider.name, provider.version),
        ).fetchone()
        if cached:
            return str(cached[0])
    # Inference never holds a SQLite writer transaction, and cancellation cannot
    # commit a partial workflow or Draft.
    run_id, started = uuid4().hex, _timestamp()
    (pipeline.root / "runtime").mkdir(exist_ok=True)
    owner_directory = pipeline.root / "runtime" / ("creator-task-" + run_id)
    owner_directory.mkdir()
    create_workspace_marker(
        pipeline.root,
        owner_directory,
        provider.name + "/" + provider.version,
        kind="CREATOR_WORKFLOW_OWNER",
    )
    first = materials[0]
    if not first.asset_id or not first.file_id:
        raise ValueError("Local generation requires physical selected material")
    with (
        closing(_connect_write(pipeline.root / "creator_loop.sqlite3")) as db,
        atomic_transaction(db),
    ):
        db.execute(
            "INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,model_name,model_version,started_at,created_at) VALUES(?,?,?,'CREATOR_GENERATION','RUNNING',?,?,?,?,?,?)",
            (
                run_id,
                first.asset_id,
                first.file_id,
                provider.name,
                provider.version,
                provider.model.component_id,
                provider.model.version,
                started,
                started,
            ),
        )
    try:
        result = (
            provider.generate(
                request,
                materials,
                cancelled,
                analysis_receipt=pipeline.workers_analysis_receipt,
            )
            if isinstance(provider, WorkersCreator)
            and pipeline.workers_analysis_receipt is not None
            else provider.generate(request, materials, cancelled)
        )
        if analysis_receipt_hash is not None:
            requests = result.get("provenance", {}).get("requests", [])
            if (
                not requests
                or requests[0].get("receipt_sha256") != analysis_receipt_hash
                or pipeline.workers_analysis_receipt is None
                or _analysis_receipt_fingerprint(
                    pipeline.root, pipeline.workers_analysis_receipt
                )
                != analysis_receipt_hash
            ):
                raise ValueError("Analysis reuse receipt changed during generation")
        if request.output_format == "VIDEO":
            # Pure scene contract validation; no RenderService/worker/engine starts.
            from creator_loop.backend.video.planner import _declared_scenes
            from creator_loop.backend.video.timeline import build_timeline
            from creator_loop.shared.workflow import RenderPreset

            scenes = _declared_scenes(result["draft_body"])
            if (
                scenes is None
                or len(scenes) != 6
                or any(not 3000 <= s.duration_ms <= 10000 for s in scenes)
            ):
                raise ValueError(
                    "Generated VIDEO must contain six valid declared scenes"
                )
            build_timeline(scenes, RenderPreset(), ())
        if cancelled():
            raise InterruptedError("Đã hủy phân tích local")
        result["provenance"]["processing_run_id"] = run_id
        return _persist(
            pipeline, request, materials, result, provider, fingerprint, run_id
        )
    except Exception as error:
        with (
            closing(_connect_write(pipeline.root / "creator_loop.sqlite3")) as db,
            atomic_transaction(db),
        ):
            db.execute(
                "UPDATE processing_runs SET status=?,finished_at=?,error_code=?,error_message=? WHERE run_id=? AND status='RUNNING'",
                (
                    "CANCELLED" if isinstance(error, InterruptedError) else "FAILED",
                    _timestamp(),
                    type(error).__name__,
                    "Local model did not complete; no workflow/Draft committed",
                    run_id,
                ),
            )
        raise


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
