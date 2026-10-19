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
            materials = [
                self.pipeline._verify(db, available[mid])
                for mid in request.material_ids
            ]
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
