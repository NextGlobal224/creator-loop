"""Local LLM workflow persistence in the existing canonical SQLite tables."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import uuid4

from creator_loop.backend.analysis.provider_selection import selected_provider
from creator_loop.backend.analysis.workers_analysis_reuse import (
    receipt_fingerprint as _analysis_receipt_fingerprint,
)
from creator_loop.backend.analysis.workers_creator import WorkersCreator
from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.runtime.database import _connect_write, open_readonly
from creator_loop.backend.runtime.runtime_ownership import create_workspace_marker
from creator_loop.backend.runtime.transactions import atomic_transaction
from creator_loop.shared.workflow import CreatorRequest

if TYPE_CHECKING:
    from creator_loop.backend.creator.research_pipeline import ResearchPipeline

from creator_loop.backend.creator.generation_records import _persist


def analyze_local(
    pipeline: ResearchPipeline, request: CreatorRequest, cancelled: Callable[[], bool]
) -> str:
    provider = selected_provider(pipeline.root, pipeline.provider_override)
    if pipeline.workers_predecessor_receipts and not isinstance(
        provider, WorkersCreator
    ):
        raise ValueError("Predecessor reuse requires an actual Workers provider")
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
        predecessor_hashes = (
            {
                stage: _analysis_receipt_fingerprint(pipeline.root, receipt)
                for stage, receipt in pipeline.workers_predecessor_receipts.items()
            }
            if isinstance(provider, WorkersCreator)
            else {}
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
                        {"predecessor_receipts_sha256": predecessor_hashes}
                        if predecessor_hashes
                        else {}
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
                predecessor_receipts=pipeline.workers_predecessor_receipts,
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
        if predecessor_hashes:
            requests = result.get("provenance", {}).get("requests", [])
            for stage, expected in predecessor_hashes.items():
                entries = [r for r in requests if r.get("stage") == stage]
                if (
                    len(entries) != 1
                    or entries[0].get("execution") != "reused_predecessor"
                    or entries[0].get("new_model_calls") != 0
                    or entries[0].get("receipt_sha256") != expected
                    or _analysis_receipt_fingerprint(
                        pipeline.root, pipeline.workers_predecessor_receipts[stage]
                    )
                    != expected
                ):
                    raise ValueError("Predecessor receipt changed during generation")
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
