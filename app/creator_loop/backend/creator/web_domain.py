"""Portable B domain adapter. The caller owns cloud durability and serialization."""

from __future__ import annotations

import hashlib
import unicodedata
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.backend.analysis.generation_contract import validate_response
from creator_loop.backend.creator.generation_records import _persist
from creator_loop.backend.creator.projects import add_project_reference, create_project
from creator_loop.backend.creator.research_pipeline import ResearchPipeline
from creator_loop.backend.knowledge.claims import _timestamp
from creator_loop.backend.knowledge.cloud_audio import correct_cloud_audio_range
from creator_loop.backend.knowledge.evidence_correction import correct_text_evidence
from creator_loop.backend.knowledge.evidence_review import current_evidence_review
from creator_loop.backend.knowledge.source_use import (
    current_source_use,
    permitted_source_versions,
    select_source_use,
)
from creator_loop.backend.knowledge.text_evidence import (
    create_text_evidence,
    read_verified_text_snapshot,
)
from creator_loop.backend.library.library import Asset, LibraryRepository, Source
from creator_loop.backend.runtime.database import _connect_write, validate
from creator_loop.backend.runtime.transactions import atomic_transaction
from creator_loop.shared.workflow import CreatorRequest

MAX_FILE_BYTES = 8 * 1024 * 1024


class WebInputError(ValueError):
    """Only fixed, safe user-facing validation messages may use this type."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class WebPipeline(ResearchPipeline):
    """Reuse read/save rules, replacing exclusively native inference ownership."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.provider_override = None
        self.workers_analysis_receipt = None
        self.workers_predecessor_receipts = {}

    def analyze(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("Web inference requires a reserved durable job")

    def materials(self) -> list[Any]:
        values = super().materials()
        with closing(_connect_write(self.root / "creator_loop.sqlite3")) as db:
            links = {
                fid: (sid, url)
                for fid, sid, url in db.execute(
                    "SELECT f.file_id,s.source_id,s.canonical_url FROM asset_files f JOIN source_assets sa USING(asset_id) JOIN sources s USING(source_id) WHERE f.role='ORIGINAL' AND sa.relationship_type='ORIGIN'"
                )
            }
        associated = {sid for sid, _ in links.values()}
        result = []
        for material in values:
            if material.file_id in links:
                sid, url = links[material.file_id]
                material = replace(
                    material, material_id="source:" + sid, source_id=sid, source_url=url
                )
            elif material.source_id in associated:
                continue  # the actual Original replaces its URL-only projection
            result.append(material)
        return result


def request_from_json(value: dict[str, Any]) -> CreatorRequest:
    allowed = {
        "project_id",
        "mode",
        "purpose",
        "audience",
        "style",
        "output_format",
        "material_ids",
        "direction",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unknown Creator request fields")
    for key in ("project_id", "purpose", "audience", "style", "direction"):
        if not isinstance(value.get(key, ""), str) or len(value.get(key, "")) > 2000:
            raise ValueError("Invalid bounded Creator text")
    ids = value.get("material_ids")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 8
        or any(not isinstance(i, str) for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("Select one to eight distinct saved materials")
    if value.get("mode") not in ("AUTOMATIC", "MANUAL") or value.get(
        "output_format"
    ) not in ("TEXT", "IMAGE_CAPTION", "VIDEO"):
        raise ValueError("Invalid Creator mode or format")
    if not value.get("purpose", "").strip() or (
        value["mode"] == "MANUAL" and not value.get("direction", "").strip()
    ):
        raise ValueError("Purpose and explicit manual direction are required")
    return CreatorRequest(**{**value, "material_ids": tuple(ids), "actor": "web-owner"})


class WebDomain:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "creator_loop.sqlite3"
        self.pipeline = WebPipeline(root)

    def _project_original(self, project_id: str, file_id: str) -> dict[str, Any]:
        material = next(
            (m for m in self.state(project_id)["materials"] if m["file_id"] == file_id),
            None,
        )
        if material is None:
            raise WebInputError("Nguồn chưa liên kết với Project đang chọn.")
        return material

    def source_review(self, project_id: str, file_id: str) -> dict[str, Any]:
        material = self._project_original(project_id, file_id)
        with closing(_connect_write(self.path)) as db:
            text = None
            if material["media_type"] == "TEXT":
                _, text = read_verified_text_snapshot(db, file_id, self.root)
            evidence = []
            for row in db.execute(
                """SELECT v.evidence_version_id,v.evidence_id,v.version_no,v.content,
                   v.locator_type,v.locator_data,v.producer_type
                   FROM evidence_versions v JOIN evidences e USING(evidence_id)
                   WHERE v.anchor_file_id=? AND e.deleted_at IS NULL
                   ORDER BY v.evidence_id,v.version_no""",
                (file_id,),
            ):
                item = dict(
                    zip(
                        (
                            "evidence_version_id",
                            "evidence_id",
                            "version_no",
                            "content",
                            "locator_type",
                            "locator_data",
                            "producer_type",
                        ),
                        row,
                    )
                )
                item["review_state"] = current_evidence_review(db, row[0])
                evidence.append(item)
            return {
                "project_id": project_id,
                "file_id": file_id,
                "media_type": material["media_type"],
                "text": text,
                "evidence_versions": evidence,
                "source_use": current_source_use(db, project_id, file_id),
                "factual_approval": False,
                "publication_approval": False,
            }

    def source_text_range(self, data: dict[str, Any]) -> dict[str, Any]:
        project_id, file_id = data.get("project_id", ""), data.get("file_id", "")
        start, end = data.get("start"), data.get("end")
        if type(start) is not int or type(end) is not int:
            raise WebInputError("Chọn phạm vi ký tự nguyên trong bản nguồn.")
        reason = data.get("reason", "Sửa phạm vi nguồn")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise WebInputError("Ghi lý do sửa phạm vi từ 1 đến 2.000 ký tự.")
        material = self._project_original(project_id, file_id)
        if material["media_type"] != "TEXT":
            raise WebInputError("Chọn đoạn nguyên văn chỉ dùng cho nguồn TEXT.")
        with closing(_connect_write(self.path)) as db:
            if data.get("evidence_version_id"):
                old = db.execute(
                    "SELECT anchor_file_id FROM evidence_versions WHERE evidence_version_id=?",
                    (data["evidence_version_id"],),
                ).fetchone()
                if old is None or old[0] != file_id:
                    raise WebInputError("Đoạn đang sửa không thuộc nguồn này.")
                corrected = correct_text_evidence(
                    db,
                    evidence_version_id=data["evidence_version_id"],
                    data_root=self.root,
                    start=start,
                    end=end,
                    actor="WEB_CLIENT",
                    reason=reason,
                )
                content = db.execute(
                    "SELECT content FROM evidence_versions WHERE evidence_version_id=?",
                    (corrected.new_version_id,),
                ).fetchone()[0]
                return {
                    "evidence_version_id": corrected.new_version_id,
                    "content": content,
                    "review_state": "PENDING",
                }
            version = create_text_evidence(
                db,
                file_id=file_id,
                data_root=self.root,
                start=start,
                end=end,
                actor="WEB_CLIENT",
            )
        return {
            "evidence_version_id": version.evidence_version_id,
            "content": version.content,
            "review_state": "PENDING",
        }

    def source_audio_range(self, data: dict[str, Any]) -> dict[str, Any]:
        project_id, file_id = data.get("project_id", ""), data.get("file_id", "")
        start, end, content = (
            data.get("start_ms"),
            data.get("end_ms"),
            data.get("content"),
        )
        if (
            type(start) is not int
            or type(end) is not int
            or not isinstance(content, str)
        ):
            raise WebInputError("Nhập transcript và khoảng thời gian mili giây nguyên.")
        material = self._project_original(project_id, file_id)
        if material["media_type"] != "AUDIO":
            raise WebInputError("Sửa transcript hiện hỗ trợ nguồn ghi âm PCM WAV.")
        with closing(_connect_write(self.path)) as db:
            row = db.execute(
                "SELECT anchor_file_id FROM evidence_versions WHERE evidence_version_id=?",
                (data.get("evidence_version_id", ""),),
            ).fetchone()
            if row is None or row[0] != file_id:
                raise WebInputError("Đoạn đang sửa không thuộc nguồn này.")
            version = correct_cloud_audio_range(
                db,
                data_root=self.root,
                evidence_version_id=data.get("evidence_version_id", ""),
                start_ms=start,
                end_ms=end,
                content=content,
                actor="WEB_CLIENT",
                reason=data.get("reason", ""),
            )
        return {"evidence_version_id": version, "review_state": "PENDING"}

    def source_use(self, data: dict[str, Any]) -> dict[str, Any]:
        project_id, file_id = data.get("project_id", ""), data.get("file_id", "")
        self._project_original(project_id, file_id)
        # Separate source-use permission from Evidence ACCEPT/publication Approval.
        if data.get("explicit_source_use_choice") is not True:
            raise WebInputError("Xác nhận lựa chọn phần nguồn trước khi lưu.")
        versions = data.get("evidence_version_ids")
        if not isinstance(versions, list):
            raise WebInputError("Chọn danh sách phiên bản nguồn hợp lệ.")
        with closing(_connect_write(self.path)) as db:
            return select_source_use(
                db,
                data_root=self.root,
                project_id=project_id,
                file_id=file_id,
                state=data.get("state", ""),
                evidence_version_ids=versions,
                expected_parent=data.get("expected_parent"),
                actor="WEB_CLIENT",
                reason=data.get("reason", ""),
            )

    def state(self, project_id: str | None = None) -> dict[str, Any]:
        with closing(_connect_write(self.path)) as db:
            projects = [asdict(p) for p in self.pipeline.projects()]
            if project_id is None:
                project_id = projects[0]["project_id"] if projects else None
            elif not any(p["project_id"] == project_id for p in projects):
                raise WebInputError("Project không còn khả dụng; chọn lại Project.")
            references = db.execute(
                "SELECT project_id,asset_id,source_id FROM project_references"
            ).fetchall()
            names = dict(db.execute("SELECT asset_id,display_name FROM assets"))
            library = []
            for m in self.pipeline.materials():
                item = asdict(m)
                item["project_ids"] = sorted(
                    {
                        pid
                        for pid, aid, sid in references
                        if (aid is not None and aid == m.asset_id)
                        or (sid is not None and sid == m.source_id)
                    }
                )
                item["display_name"] = names.get(
                    m.asset_id, m.source_url or m.material_id
                )
                library.append(item)
            materials = [m for m in library if project_id in m["project_ids"]]
            for material in materials:
                use = (
                    current_source_use(db, project_id, material["file_id"])
                    if material["file_id"] and project_id
                    else None
                )
                material["source_use"] = use
                material["usable_for_draft"] = False
                material["source_use_message"] = "Mở nguồn và xác nhận phần được dùng."
                if use and use["state"] == "SELECTED" and project_id:
                    try:
                        permitted_source_versions(
                            db,
                            data_root=self.root,
                            project_id=project_id,
                            file_id=material["file_id"],
                        )
                    except ValueError as exc:
                        material["source_use_message"] = str(exc)
                    else:
                        material["usable_for_draft"] = True
                        material["source_use_message"] = (
                            "Phần nguồn đã chọn còn hiệu lực."
                        )
            files = {m["file_id"] for m in materials}
            return {
                "projects": projects,
                "project_id": project_id,
                "materials": materials,
                "library_materials": library,
                "images": [
                    i for i in self.pipeline.image_options() if i["file_id"] in files
                ],
                "workflows": [
                    dict(
                        zip(
                            (
                                "workflow_id",
                                "project_id",
                                "mode",
                                "output_format",
                                "purpose",
                                "created_at",
                            ),
                            r,
                        )
                    )
                    for r in db.execute(
                        "SELECT workflow_id,project_id,mode,output_format,purpose,created_at FROM creator_workflows WHERE project_id=? ORDER BY rowid DESC",
                        (project_id,),
                    )
                ],
                "review": "UNREVIEWED — duyệt thủ công trước đăng; không tự ACCEPT/APPROVE",
            }

    def link_material(self, project_id: str, material_id: str) -> dict[str, str]:
        material = next(
            (m for m in self.pipeline.materials() if m.material_id == material_id), None
        )
        if material is None:
            raise WebInputError(
                "Tư liệu thư viện không còn khả dụng; tải lại danh sách."
            )
        with closing(_connect_write(self.path)) as db, atomic_transaction(db):
            target = (
                {"asset_id": material.asset_id}
                if material.asset_id
                else {"source_id": material.source_id}
            )
            if not any(target.values()):
                raise WebInputError("Tư liệu chưa có Asset/Source để liên kết Project.")
            column, value = next(iter(target.items()))
            if not db.execute(
                f"SELECT 1 FROM project_references WHERE project_id=? AND {column}=?",
                (project_id, value),
            ).fetchone():
                add_project_reference(
                    db, project_id=project_id, usage_intent="RESEARCH", **target
                )
        return {"project_id": project_id, "material_id": material_id}

    def require_project_image(self, project_id: str, file_id: str) -> None:
        if not any(i["file_id"] == file_id for i in self.state(project_id)["images"]):
            raise WebInputError(
                "Ảnh chưa liên kết với Project này. Thêm ảnh từ thư viện hoặc tải ảnh vào Project trước."
            )

    def project(self, title: str) -> dict[str, Any]:
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("Project title must contain 1–200 characters")
        with closing(_connect_write(self.path)) as db:
            return asdict(create_project(db, title=title))

    def intake(self, metadata: dict[str, Any], raw: bytes) -> dict[str, str]:
        media_type = metadata.get("media_type")
        if (
            media_type not in ("TEXT", "IMAGE", "VIDEO", "AUDIO")
            or not 0 < len(raw) <= MAX_FILE_BYTES
        ):
            raise WebInputError(
                "Chọn tệp không rỗng, tối đa 8 MiB và đúng loại tư liệu."
            )
        name = metadata.get("display_name", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise WebInputError("Nhập tên tư liệu từ 1 đến 200 ký tự.")
        if media_type == "TEXT":
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise WebInputError(
                    "Tệp TEXT phải là văn bản UTF-8; ảnh chọn loại IMAGE (PNG/JPEG/WebP)."
                ) from None
            if not text.strip() or text != unicodedata.normalize("NFC", text):
                raise WebInputError(
                    "Text phải có nội dung UTF-8 chuẩn NFC. Lưu lại tệp đúng chuẩn hoặc dán nội dung vào ô Text; không tự đổi byte gốc."
                )
            mime = "text/plain"
        elif media_type == "IMAGE":
            if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif raw.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                raise WebInputError(
                    "Ảnh chỉ hỗ trợ PNG, JPEG hoặc WebP; không nhận SVG/HTML hoặc tệp chỉ đổi đuôi."
                )
        else:
            # Opaque downloadable source bytes; no decoded-content/ASR claim.
            mime = "application/octet-stream"
        details = metadata.get("source", {})
        if not isinstance(details, dict) or set(details) - {
            "url",
            "publisher",
            "rights",
        }:
            raise ValueError("Invalid source metadata")
        rights = details.get("rights", "UNKNOWN")
        if rights not in (
            "UNKNOWN",
            "OWNED",
            "LICENSED",
            "REFERENCE_ONLY",
            "RESTRICTED",
        ):
            raise ValueError("Unknown source rights")
        url = details.get("url") or None
        if url is not None and (
            not isinstance(url, str)
            or len(url) > 2000
            or not url.startswith(("https://", "http://"))
        ):
            raise ValueError("Source URL must use HTTP(S)")
        publisher = details.get("publisher") or None
        if publisher is not None and (
            not isinstance(publisher, str) or len(publisher) > 200
        ):
            raise ValueError("Invalid source publisher")
        digest = hashlib.sha256(raw).hexdigest()
        fid, aid, sid, now = uuid4().hex, uuid4().hex, uuid4().hex, _timestamp()
        key = "storage/originals/" + digest
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != raw:
                raise ValueError("Content-addressed original mismatch")
        else:
            destination.write_bytes(raw)
        with closing(_connect_write(self.path)) as db, atomic_transaction(db):
            repo = LibraryRepository(db)
            repo.create_asset(Asset(aid, media_type, name.strip(), now, None))
            db.execute(
                "INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at) VALUES(?,?,'ORIGINAL',?,?,?,?,?)",
                (fid, aid, key, digest, len(raw), mime, now),
            )
            repo.create_source(
                Source(sid, "WEB_IMPORT", url, None, publisher, None, now, rights, now)
            )
            repo.link_source_asset(
                source_id=sid,
                asset_id=aid,
                relationship_type="ORIGIN",
                verification_status="UNREVIEWED",
                recorded_at=now,
            )
            if metadata.get("project_id"):
                add_project_reference(
                    db,
                    project_id=metadata["project_id"],
                    asset_id=aid,
                    usage_intent="RESEARCH",
                )
        return {
            "file_id": fid,
            "asset_id": aid,
            "source_id": sid,
            "sha256": digest,
            "storage_key": key,
        }

    def prepare(self, request: CreatorRequest) -> list[Any]:
        available = {m.material_id: m for m in self.pipeline.materials()}
        if any(mid not in available for mid in request.material_ids):
            raise ValueError("Selected saved material is unavailable")
        with closing(_connect_write(self.path)) as db:
            if not db.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND status='ACTIVE' AND deleted_at IS NULL",
                (request.project_id,),
            ).fetchone():
                raise ValueError("Active Project required")
            linked = {
                m["material_id"] for m in self.state(request.project_id)["materials"]
            }
            if any(mid not in linked for mid in request.material_ids):
                raise WebInputError(
                    "Tư liệu chưa liên kết với Project đang chọn; thêm từ thư viện trước khi tạo AI."
                )
            materials = []
            for mid in request.material_ids:
                material = available[mid]
                if not material.file_id:
                    raise WebInputError(
                        "Tư liệu chưa có bản gốc để đối chiếu; nhập/tải nguồn trước."
                    )
                try:
                    chosen = permitted_source_versions(
                        db,
                        data_root=self.root,
                        project_id=request.project_id,
                        file_id=material.file_id,
                    )
                except ValueError:
                    raise WebInputError(
                        "Mở nguồn và chọn/xác nhận các đoạn hiện tại được dùng cho Draft; nguồn chưa chọn, đã bỏ hoặc phiên bản đã đổi."
                    ) from None
                material = replace(
                    material,
                    text="\n".join(text for _, text in chosen),
                    evidence_version_ids=tuple(vid for vid, _ in chosen),
                    scope="USER_SELECTED_SOURCE_VERSIONS",
                )
                materials.append(self.pipeline._verify(db, material))
        if (
            any(not m.text.strip() for m in materials)
            or sum(len(m.text) for m in materials) > 10000
        ):
            raise ValueError(
                "Generation needs bounded verified text; unprocessed media is not textual evidence"
            )
        return materials

    def finish(
        self,
        request: CreatorRequest,
        result: dict[str, Any],
        provenance: dict[str, Any],
        fingerprint: str,
    ) -> str:
        materials = self.prepare(request)
        result = validate_response(result, materials)
        if request.output_format == "VIDEO":
            from creator_loop.backend.video.planner import _declared_scenes
            from creator_loop.backend.video.timeline import build_timeline
            from creator_loop.shared.workflow import RenderPreset

            scenes = _declared_scenes(result["draft_body"])
            if (
                scenes is None
                or len(scenes) != 6
                or any(not 3000 <= s.duration_ms <= 10000 for s in scenes)
            ):
                raise ValueError("VIDEO needs six declared scenes with valid timing")
            build_timeline(scenes, RenderPreset(), ())
        result["provenance"] = provenance
        provider: Any = type(
            "CloudProvider",
            (),
            {"name": "CLOUDFLARE_WORKERS_AI_WEB", "version": provenance["version"]},
        )()
        wid = _persist(
            self.pipeline,
            request,
            materials,
            result,
            provider,
            fingerprint,
            provenance["run_id"],
        )
        with closing(_connect_write(self.path)) as db:
            validate(db)
        return wid

    def begin_run(
        self, request: CreatorRequest, run_id: str, version: str, model: str
    ) -> None:
        materials = self.prepare(request)
        first = materials[0]
        with closing(_connect_write(self.path)) as db, atomic_transaction(db):
            db.execute(
                "INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,model_name,model_version,created_at) VALUES(?,?,?,'CREATOR_GENERATION','QUEUED','CLOUDFLARE_WORKERS_AI_WEB',?,?,?,?)",
                (
                    run_id,
                    first.asset_id,
                    first.file_id,
                    version,
                    model,
                    version,
                    _timestamp(),
                ),
            )

    def draft(self, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "workflow_id",
            "direction",
            "body_text",
            "image_file_id",
            "image_provenance",
            "image_usage_note",
        }
        if (
            not isinstance(value, dict)
            or set(value) - allowed
            or not isinstance(value.get("workflow_id"), str)
        ):
            raise ValueError("Invalid Draft save fields")
        if any(not isinstance(v, (str, type(None))) for v in value.values()) or any(
            isinstance(v, str) and len(v) > 24000 for v in value.values()
        ):
            raise ValueError("Invalid bounded Draft text")
        return asdict(self.pipeline.create_draft(**value, actor="web-owner"))
