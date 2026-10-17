"""SQLite-backed, shared automatic/manual research-to-Draft pipeline."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.backend.analysis.content_analysis import (
    BACKEND_NAME,
    BACKEND_VERSION,
    analyze_material,
    draft_body,
    recommend_direction,
)
from creator_loop.backend.creator.drafts import (
    DraftAssertion,
    DraftClaimLink,
    append_draft_version,
    create_draft,
)
from creator_loop.backend.creator.projects import list_projects
from creator_loop.backend.knowledge.claims import EvidenceLink, _timestamp, create_claim
from creator_loop.backend.knowledge.evidence import (
    Evidence,
    EvidenceRepository,
    EvidenceVersion,
)
from creator_loop.backend.knowledge.evidence_reopen import reopen_evidence_version
from creator_loop.backend.knowledge.text_evidence import read_verified_text_snapshot
from creator_loop.backend.library.originals import verify_original_file
from creator_loop.backend.runtime.database import _connect_write, open_readonly
from creator_loop.backend.runtime.transactions import atomic_transaction
from creator_loop.shared.workflow import CreatorRequest, DraftSnapshot, ResearchMaterial


class ResearchPipeline:
    def __init__(
        self,
        root: Path,
        *,
        provider_override: str | None = None,
        workers_analysis_receipt: Path | None = None,
        workers_predecessor_receipts: Mapping[str, Path] | None = None,
    ) -> None:
        self.root = Path(root)
        if provider_override not in (
            None,
            "LOCAL_LLAMA_CPP",
            "CODEX_CLI_TEXT",
            "CLOUDFLARE_WORKERS_AI",
        ):
            raise ValueError("Unknown B provider")
        self.provider_override = provider_override
        if (
            workers_analysis_receipt is not None
            and provider_override != "CLOUDFLARE_WORKERS_AI"
        ):
            raise ValueError("Analysis reuse requires an explicit Workers provider")
        self.workers_analysis_receipt = workers_analysis_receipt
        self.workers_predecessor_receipts = dict(workers_predecessor_receipts or {})
        if self.workers_predecessor_receipts and (
            provider_override != "CLOUDFLARE_WORKERS_AI"
            or workers_analysis_receipt is None
            or set(self.workers_predecessor_receipts)
            not in ({"writing"}, {"writing", "editing"})
        ):
            raise ValueError("Predecessor reuse requires an explicit Workers prefix")
        from creator_loop.backend.creator.generation_recovery import recover_generation

        recover_generation(self.root)

    def projects(self) -> list[Any]:
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            return list_projects(db)

    def evidence_snapshot(self, version_id: str) -> dict[str, Any]:
        """Small exact-version metadata for B; no decoding/hashing on the UI."""
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT v.*,f.sha256 FROM evidence_versions v JOIN asset_files f ON f.file_id=v.anchor_file_id WHERE v.evidence_version_id=?",
                (version_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Exact Evidence Version unavailable")
            result = dict(row)
            result["sources"] = [
                dict(r)
                for r in db.execute(
                    "SELECT s.canonical_url,s.publisher_name FROM sources s JOIN source_assets sa USING(source_id) WHERE sa.asset_id=?",
                    (row["asset_id"],),
                )
            ]
            result["review"] = (
                "Metadata snapshot only; not human acceptance or a fresh anchor/digest check"
            )
            return result

    def materials(self) -> list[ResearchMaterial]:
        """Saved Sources and actual originals; discovery candidates are excluded."""
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            rows = db.execute("""SELECT s.source_id,s.canonical_url,c.snapshot_file_id,c.excerpt,f.asset_id,f.sha256,a.media_type
                FROM sources s LEFT JOIN source_captures c USING(source_id)
                LEFT JOIN asset_files f ON f.file_id=c.snapshot_file_id LEFT JOIN assets a ON a.asset_id=f.asset_id
                ORDER BY s.created_at DESC,s.source_id""").fetchall()
            result = [
                ResearchMaterial(
                    "source:" + r[0],
                    r[0],
                    r[4],
                    r[2],
                    r[5],
                    r[6] or "REFERENCE",
                    r[3] or "",
                    r[1],
                    scope="CAPTION_ONLY" if r[3] else "URL_ONLY",
                )
                for r in rows
            ]
            used = {m.file_id for m in result if m.file_id}
            for fid, aid, digest, media_type in db.execute(
                "SELECT f.file_id,f.asset_id,f.sha256,a.media_type FROM asset_files f JOIN assets a USING(asset_id) WHERE f.role='ORIGINAL' AND a.deleted_at IS NULL ORDER BY f.created_at DESC"
            ):
                if fid in used:
                    continue
                text = ""
                if media_type == "TEXT":
                    try:
                        _, text = read_verified_text_snapshot(db, fid, self.root)
                    except ValueError:
                        text = ""
                result.append(
                    ResearchMaterial(
                        "file:" + fid,
                        None,
                        aid,
                        fid,
                        digest,
                        media_type,
                        text,
                        scope="VERIFIED_TEXT"
                        if media_type == "TEXT"
                        else "UNPROCESSED_MEDIA",
                    )
                )
            projected = []
            for material in result:
                if material.media_type in ("TEXT", "IMAGE", "VIDEO", "AUDIO"):
                    evs = db.execute(
                        """SELECT v.evidence_version_id,v.content,v.locator_type,v.locator_data FROM evidence_versions v JOIN evidences e USING(evidence_id)
                        WHERE v.asset_id=? AND e.deleted_at IS NULL AND v.version_no=(SELECT MAX(version_no) FROM evidence_versions WHERE evidence_id=v.evidence_id)
                        AND NOT (v.producer_type='TOOL' AND v.version_no=1 AND EXISTS(SELECT 1 FROM processing_runs r WHERE r.run_id=v.processing_run_id AND r.tool_name=?))
                        ORDER BY v.created_at,v.evidence_version_id""",
                        (material.asset_id, BACKEND_NAME),
                    ).fetchall()
                    if evs:
                        material = replace(
                            material,
                            text="\n".join(f"[{e[2]} {e[3]}] {e[1]}" for e in evs),
                            evidence_version_ids=tuple(e[0] for e in evs),
                            scope="PROCESSED_TEXT_EVIDENCE"
                            if material.media_type == "TEXT"
                            else "PROCESSED_IMAGE_EVIDENCE"
                            if material.media_type == "IMAGE"
                            else "PROCESSED_VIDEO_EVIDENCE",
                        )
                projected.append(material)
            return projected

    def _verify(
        self, db: sqlite3.Connection, material: ResearchMaterial
    ) -> ResearchMaterial:
        if (
            material.asset_id
            and db.execute(
                "SELECT 1 FROM assets WHERE asset_id=? AND deleted_at IS NULL",
                (material.asset_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("Selected Asset is no longer live")
        if material.file_id:
            verify_original_file(db, material.file_id, self.root)
        for evidence_id in material.evidence_version_ids:
            reopen_evidence_version(db, evidence_id, self.root)
        if (
            material.media_type == "TEXT"
            and material.file_id
            and not material.evidence_version_ids
        ):
            _, snapshot = read_verified_text_snapshot(db, material.file_id, self.root)
            text = (
                unicodedata.normalize("NFC", material.text)
                if material.source_id
                else snapshot
            )
            start = snapshot.find(text)
            if not text or start < 0:
                raise ValueError(
                    "Saved excerpt no longer matches its physical original"
                )
            # One bounded passage per original; the analysis states this scope.
            return replace(
                material,
                text=text[:1200],
                scope="CAPTION_ONLY" if material.source_id else "TEXT_PASSAGE",
            )
        return material

    def _text_evidence(self, db: sqlite3.Connection, material: ResearchMaterial) -> str:
        assert material.file_id and material.asset_id
        _, snapshot = read_verified_text_snapshot(db, material.file_id, self.root)
        start = snapshot.find(material.text)
        locator = json.dumps(
            {
                "start": start,
                "end": start + len(material.text),
                "text_digest": "sha256:"
                + hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
        )
        existing = db.execute(
            "SELECT evidence_version_id FROM evidence_versions WHERE anchor_file_id=? AND content=? AND locator_data=? ORDER BY rowid DESC LIMIT 1",
            (material.file_id, material.text, locator),
        ).fetchone()
        if existing:
            reopen_evidence_version(db, existing[0], self.root)
            return str(existing[0])
        now, run_id, eid, vid = _timestamp(), uuid4().hex, uuid4().hex, uuid4().hex
        db.execute(
            """INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,started_at,finished_at,created_at)
            VALUES(?,?,?,'LOCAL_CONTENT_ANALYSIS','SUCCEEDED',?,?,?,?,?)""",
            (
                run_id,
                material.asset_id,
                material.file_id,
                BACKEND_NAME,
                BACKEND_VERSION,
                now,
                now,
                now,
            ),
        )
        version = EvidenceVersion(
            vid,
            eid,
            material.asset_id,
            1,
            material.file_id,
            material.text,
            "TEXT_RANGE",
            locator,
            "TOOL",
            run_id,
            None,
            now,
        )
        EvidenceRepository(db).create_with_version(
            Evidence(eid, material.asset_id, "DIRECT_TEXT", now, None), version
        )
        return vid

    def analyze(
        self, request: CreatorRequest, cancelled: Callable[[], bool] = lambda: False
    ) -> str:
        if request.mode not in ("AUTOMATIC", "MANUAL") or request.output_format not in (
            "TEXT",
            "IMAGE_CAPTION",
            "VIDEO",
        ):
            raise ValueError("Invalid workflow mode/format")
        if (
            not request.purpose.strip()
            or not request.actor.strip()
            or not request.material_ids
            or len(set(request.material_ids)) != len(request.material_ids)
        ):
            raise ValueError(
                "Purpose, actor and explicit distinct materials are required"
            )
        if len(request.material_ids) > 40:
            raise ValueError("Select at most 40 materials for one light analysis")
        from creator_loop.backend.analysis.provider_selection import provider_ready

        if provider_ready(self.root, self.provider_override):
            from creator_loop.backend.creator.local_generation import analyze_local

            return analyze_local(self, request, cancelled)
        if request.mode == "AUTOMATIC":
            raise ValueError(
                "Chưa chọn mô hình LLM local cho B; không thay bằng template"
            )
        available = {m.material_id: m for m in self.materials()}
        if any(mid not in available for mid in request.material_ids):
            raise ValueError("Selected saved material not found")
        with (
            closing(_connect_write(self.root / "creator_loop.sqlite3")) as db,
            atomic_transaction(db),
        ):
            if (
                db.execute(
                    "SELECT 1 FROM projects WHERE project_id=? AND status='ACTIVE' AND deleted_at IS NULL",
                    (request.project_id,),
                ).fetchone()
                is None
            ):
                raise ValueError("Active Project required")
            materials = [
                self._verify(db, available[mid]) for mid in request.material_ids
            ]
            fingerprint_data = {
                "project": request.project_id,
                "purpose": request.purpose.strip(),
                "audience": request.audience.strip(),
                "style": request.style.strip(),
                "format": request.output_format,
                "backend": (BACKEND_NAME, BACKEND_VERSION),
                "materials": [asdict(m) for m in materials],
            }
            feedback_inputs = []
            for material in materials:
                if material.source_id:
                    observation = db.execute(
                        "SELECT c.candidate_id,c.snapshot_json FROM facebook_selection_events e JOIN facebook_candidates c USING(candidate_id) WHERE e.source_id=? ORDER BY e.rowid DESC LIMIT 1",
                        (material.source_id,),
                    ).fetchone()
                    if observation:
                        feedback_inputs.append(
                            (
                                material.source_id,
                                observation[0],
                                hashlib.sha256(
                                    observation[1].encode("utf-8")
                                ).hexdigest(),
                            )
                        )
            fingerprint_data["feedback_inputs"] = feedback_inputs
            fingerprint = hashlib.sha256(
                json.dumps(fingerprint_data, ensure_ascii=False, sort_keys=True).encode(
                    "utf-8"
                )
            ).hexdigest()
            cached = db.execute(
                "SELECT workflow_id FROM creator_workflows WHERE input_fingerprint=? AND project_id=? AND backend_name=? AND backend_version=? ORDER BY rowid DESC LIMIT 1",
                (fingerprint, request.project_id, BACKEND_NAME, BACKEND_VERSION),
            ).fetchone()
            if cached:
                return str(cached[0])
            prepared = []
            for material in materials:
                if (
                    material.media_type == "TEXT"
                    and material.file_id
                    and material.text
                    and not material.evidence_version_ids
                ):
                    material = replace(
                        material,
                        evidence_version_ids=(self._text_evidence(db, material),),
                    )
                prepared.append(material)
            analysis = [analyze_material(m) for m in prepared]
            analysis_text = f"Backend {BACKEND_NAME}/{BACKEND_VERSION}. Không có LLM sinh nội dung; mô tả cấu trúc local và mẫu editorial.\n"
            for result in analysis:
                analysis_text += f"\nTư liệu {result.material_id}\nPhạm vi {result.scope}\nMở đầu đọc được: {result.hook}\nCấu trúc: {result.structure}\n{result.message}\nDữ kiện cần kiểm chứng: {'; '.join(result.data_cues) or 'Chưa rõ'}\nEvidence Versions: {', '.join(result.evidence_version_ids) or 'Chưa có'}\nGiới hạn: {result.limitation}\n"
                material = next(
                    m for m in prepared if m.material_id == result.material_id
                )
                captured = (
                    db.execute(
                        "SELECT c.candidate_id,c.snapshot_json FROM facebook_selection_events e JOIN facebook_candidates c USING(candidate_id) WHERE e.source_id=? ORDER BY e.rowid DESC LIMIT 1",
                        (material.source_id,),
                    ).fetchone()
                    if material.source_id
                    else None
                )
                if captured:
                    post = json.loads(captured[1])
                    feedback = "; ".join(
                        f"{key}: "
                        + (
                            value["raw"] + (" (xấp xỉ)" if value["approximate"] else "")
                            if value
                            else "Chưa rõ"
                        )
                        for key, value in post["metrics"].items()
                    )
                    analysis_text += f"Phản hồi quan sát từ {post['url']} — candidate {captured[0]}, thu {post['observed_at']}: {feedback}. Nội dung bình luận: Chưa rõ. Chỉ quan sát, chưa xác minh nguyên nhân tạo tương tác.\n"
                else:
                    analysis_text += "Phản hồi/số liệu tương tác: Chưa rõ — chưa có snapshot Facebook gắn đúng Source.\n"
            wid, now = uuid4().hex, _timestamp()
            direction = request.direction.strip() or recommend_direction(
                request.purpose, request.audience, request.style
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
                    BACKEND_NAME,
                    BACKEND_VERSION,
                    fingerprint,
                    analysis_text,
                    direction,
                    now,
                ),
            )
            position = 0
            for material in prepared:
                anchors = {
                    ev: db.execute(
                        "SELECT v.anchor_file_id,f.sha256 FROM evidence_versions v JOIN asset_files f ON f.file_id=v.anchor_file_id WHERE v.evidence_version_id=?",
                        (ev,),
                    ).fetchone()
                    for ev in material.evidence_version_ids
                }
                refs: tuple[str | None, ...] = material.evidence_version_ids or (None,)
                if material.file_id and any(
                    row[0] != material.file_id for row in anchors.values()
                ):
                    refs = (None,) + refs
                for ev in refs:
                    fid, digest = material.file_id, material.sha256
                    if ev:
                        fid, digest = anchors[ev]
                    db.execute(
                        "INSERT INTO creator_workflow_materials VALUES(?,?,?,?,?,?)",
                        (wid, position, material.source_id, fid, digest, ev),
                    )
                    position += 1
                if material.evidence_version_ids:
                    claim = create_claim(
                        db,
                        claim_type="EDITORIAL_HYPOTHESIS",
                        statement=f"Giả thuyết editorial: thử cấu trúc hỏi–kiểm chứng–điều chưa rõ cho mục đích “{request.purpose.strip()}”, dựa trên phần {material.scope} của tư liệu {material.material_id}; chưa xác nhận nguyên nhân tương tác.",
                        actor=BACKEND_NAME,
                        links=[
                            EvidenceLink(e, "CONTEXT")
                            for e in material.evidence_version_ids
                        ],
                    )
                    db.execute(
                        "INSERT INTO creator_workflow_claims VALUES(?,?)",
                        (wid, claim.claim_version_id),
                    )
            return wid

    def workflows(self, project_id: str) -> list[dict[str, Any]]:
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            return [
                dict(
                    zip(
                        ("workflow_id", "mode", "format", "purpose", "created_at"),
                        row,
                        strict=True,
                    )
                )
                for row in db.execute(
                    "SELECT workflow_id,mode,output_format,purpose,created_at FROM creator_workflows WHERE project_id=? ORDER BY rowid DESC",
                    (project_id,),
                )
            ]

    def load(self, workflow_id: str) -> dict[str, Any]:
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM creator_workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Workflow not found")
            result = dict(row)
            from creator_loop.backend.creator.local_generation import (
                decode_analysis,
                display_analysis,
            )

            proposal = decode_analysis(result["analysis_text"])
            if proposal is not None:
                result["llm_proposal"] = proposal
                result["analysis_text"] = display_analysis(proposal)
            result["materials"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM creator_workflow_materials WHERE workflow_id=? ORDER BY position",
                    (workflow_id,),
                )
            ]
            ids = []
            for material in result["materials"]:
                if material["source_id"]:
                    ids.append("source:" + material["source_id"])
                elif db.execute(
                    "SELECT 1 FROM asset_files WHERE file_id=? AND role='ORIGINAL'",
                    (material["file_id"],),
                ).fetchone():
                    ids.append("file:" + material["file_id"])
            result["material_ids"] = list(dict.fromkeys(ids))
            result["claims"] = [
                str(r[0])
                for r in db.execute(
                    "SELECT claim_version_id FROM creator_workflow_claims WHERE workflow_id=? ORDER BY rowid",
                    (workflow_id,),
                )
            ]
            result["drafts"] = [
                str(r[0])
                for r in db.execute(
                    "SELECT draft_version_id FROM creator_workflow_drafts WHERE workflow_id=? ORDER BY rowid DESC",
                    (workflow_id,),
                )
            ]
            return result

    def draft(self, draft_version_id: str) -> DraftSnapshot:
        with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
            row = db.execute(
                "SELECT v.draft_version_id,d.project_id,v.version_no,v.body_text,v.format FROM draft_versions v JOIN drafts d USING(draft_id) JOIN draft_version_seals s USING(draft_version_id) WHERE v.draft_version_id=?",
                (draft_version_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Sealed Draft not found")
            claims = tuple(
                str(r[0])
                for r in db.execute(
                    "SELECT claim_version_id FROM draft_claims WHERE draft_version_id=? ORDER BY claim_version_id",
                    (draft_version_id,),
                )
            )
            return DraftSnapshot(row[0], row[1], row[2], row[3], row[4], claims)

    def create_draft(
        self,
        workflow_id: str,
        direction: str = "",
        *,
        body_text: str = "",
        actor: str = "creator",
        claims: Sequence[DraftClaimLink] | None = None,
        assertions: Sequence[DraftAssertion] | None = None,
    ) -> DraftSnapshot:
        workflow = self.load(workflow_id)
        reuse_existing = False
        if (
            workflow["drafts"]
            and not body_text
            and claims is None
            and assertions is None
        ):
            # An unmodified manual editor must not regenerate a template over
            # the saved content merely because the Save button was clicked.
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as existing:
                author = existing.execute(
                    "SELECT created_by FROM draft_versions WHERE draft_version_id=?",
                    (workflow["drafts"][0],),
                ).fetchone()[0]
            if (
                author != workflow["backend_name"]
                or not direction.strip()
                or direction.strip() == workflow["direction_text"]
            ):
                reuse_existing = True
        if not workflow["claims"]:
            raise ValueError(
                "Chưa có Evidence của nội dung đã xử lý. URL-only không đủ tạo Draft có căn cứ."
            )
        for material in workflow["materials"]:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as check:
                if material["evidence_version_id"]:
                    reopen_evidence_version(
                        check, material["evidence_version_id"], self.root
                    )
                elif material["file_id"]:
                    verify_original_file(check, material["file_id"], self.root)
        if reuse_existing:
            return self.draft(workflow["drafts"][0])
        chosen = direction.strip() or workflow["direction_text"]
        proposal = workflow.get("llm_proposal")
        if proposal and not body_text and chosen != workflow["direction_text"]:
            raise ValueError(
                "Hướng mới cần phân tích mới hoặc chỉnh Draft thủ công; giữ phiên bản cũ"
            )
        body = body_text.strip() or (
            proposal["draft_body"]
            if proposal
            else draft_body(
                direction=chosen,
                purpose=workflow["purpose"],
                audience=workflow["audience"],
                style=workflow["style"],
                output_format=workflow["output_format"],
                citations=tuple(workflow["claims"]),
            )
        )
        links = [DraftClaimLink(cid, "INSPIRATION") for cid in workflow["claims"]]
        if body_text and claims is None and workflow["drafts"]:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as previous:
                links = [
                    DraftClaimLink(cid, use)
                    for cid, use in previous.execute(
                        "SELECT claim_version_id,use_type FROM draft_claims WHERE draft_version_id=?",
                        (workflow["drafts"][0],),
                    )
                ]
        if claims is not None:
            by_version = {link.claim_version_id: link for link in links}
            by_version.update({link.claim_version_id: link for link in claims})
            links = list(by_version.values())
        # Manually authored factual passages must keep their exact Claim Version
        # and review state, rather than label the entire body EDITORIAL.
        authored_assertions = (
            list(assertions)
            if assertions is not None
            else [
                DraftAssertion(
                    0, len(body), None, "NEEDS_SOURCE" if body_text else "EDITORIAL"
                )
            ]
        )
        if body_text and assertions is None and workflow["drafts"]:
            old = self.draft(workflow["drafts"][0])
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as previous:
                anchored = []
                for start, end, cid in previous.execute(
                    "SELECT text_start,text_end,claim_version_id FROM draft_assertions WHERE draft_version_id=? AND claim_version_id IS NOT NULL",
                    (old.draft_version_id,),
                ):
                    passage = old.body_text[start:end]
                    if passage and body.count(passage) == 1:
                        at = body.index(passage)
                        anchored.append((at, at + len(passage), cid))
            if anchored:
                authored_assertions = []
                position = 0
                for start, end, cid in sorted(anchored):
                    if start < position:
                        raise ValueError(
                            "Overlapping unchanged factual spans need explicit manual citations"
                        )
                    if start > position:
                        authored_assertions.append(
                            DraftAssertion(position, start, None, "NEEDS_SOURCE")
                        )
                    authored_assertions.append(
                        DraftAssertion(start, end, cid, "UNREVIEWED")
                    )
                    position = end
                if position < len(body):
                    authored_assertions.append(
                        DraftAssertion(position, len(body), None, "NEEDS_SOURCE")
                    )
        if proposal and not body_text and assertions is None:
            authored_assertions = []
            factual = sorted(
                proposal["factual_claims"], key=lambda c: body.index(c["text"])
            )
            position = 0
            for claim in factual:
                start = body.index(claim["text"])
                end = start + len(claim["text"])
                if start < position:
                    raise ValueError(
                        "Overlapping factual passages need manual correction"
                    )
                if start > position:
                    authored_assertions.append(
                        DraftAssertion(position, start, None, "NEEDS_SOURCE")
                    )
                authored_assertions.append(
                    DraftAssertion(start, end, claim["claim_version_id"], "UNREVIEWED")
                )
                links = [
                    DraftClaimLink(
                        link.claim_version_id,
                        "ASSERTED"
                        if link.claim_version_id == claim["claim_version_id"]
                        else link.use_type,
                    )
                    for link in links
                ]
                position = end
            if position < len(body):
                authored_assertions.append(
                    DraftAssertion(position, len(body), None, "NEEDS_SOURCE")
                )
        with (
            closing(_connect_write(self.root / "creator_loop.sqlite3")) as db,
            atomic_transaction(db),
        ):
            latest = db.execute(
                "SELECT draft_version_id FROM creator_workflow_drafts WHERE workflow_id=? ORDER BY rowid DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if latest:
                current = self.draft(latest[0])
                old_links = set(
                    db.execute(
                        "SELECT claim_version_id,use_type FROM draft_claims WHERE draft_version_id=?",
                        (current.draft_version_id,),
                    )
                )
                old_assertions = set(
                    db.execute(
                        "SELECT text_start,text_end,claim_version_id,review_state FROM draft_assertions WHERE draft_version_id=?",
                        (current.draft_version_id,),
                    )
                )
                if (
                    current.body_text == body
                    and old_links
                    == {(link.claim_version_id, link.use_type) for link in links}
                    and old_assertions
                    == {
                        (a.start, a.end, a.claim_version_id, a.review_state)
                        for a in authored_assertions
                    }
                ):
                    return current
                saved = append_draft_version(
                    db,
                    draft_version_id=current.draft_version_id,
                    body_text=body,
                    format=workflow["output_format"],
                    actor=actor if body_text else workflow["backend_name"],
                    claims=links,
                    assertions=authored_assertions,
                )
            else:
                saved = create_draft(
                    db,
                    project_id=workflow["project_id"],
                    body_text=body,
                    format=workflow["output_format"],
                    actor=actor if body_text else workflow["backend_name"],
                    claims=links,
                    assertions=authored_assertions,
                )
            db.execute(
                "INSERT INTO creator_workflow_drafts VALUES(?,?,?)",
                (workflow_id, saved.draft_version_id, _timestamp()),
            )
        return self.draft(saved.draft_version_id)
