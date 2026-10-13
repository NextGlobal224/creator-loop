"""Draft render orchestration and SQLite provenance; no implicit execution."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from creator_loop.backend.library.storage_paths import resolve_storage_path
from creator_loop.backend.library.text_intake import intake_text_original
from creator_loop.backend.publication.publication_media import hold_publication_media
from creator_loop.backend.runtime.database import _connect_write, open_readonly
from creator_loop.backend.runtime.owned_process import _api
from creator_loop.backend.runtime.paths import ensure_data_root
from creator_loop.backend.runtime.runtime_ownership import process_identity
from creator_loop.backend.runtime.runtime_recovery import _identity_dead
from creator_loop.backend.video.component_config import load_video_components
from creator_loop.shared.workflow import (
    DraftSnapshot,
    Progress,
    RenderPreset,
    RenderRequest,
    RenderResult,
    Scene,
    Subtitle,
    VerifiedMedia,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class RenderService:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.db_path = self.root / "creator_loop.sqlite3"

    def drafts(self) -> tuple[DraftSnapshot, ...]:
        with closing(open_readonly(self.db_path)) as db:
            rows = db.execute(
                "SELECT v.draft_version_id,d.project_id,v.version_no,v.body_text,v.format FROM draft_versions v JOIN drafts d ON d.draft_id=v.draft_id JOIN draft_version_seals s ON s.draft_version_id=v.draft_version_id WHERE v.format='VIDEO' AND d.status='ACTIVE' ORDER BY v.rowid DESC"
            ).fetchall()
            return tuple(
                DraftSnapshot(
                    r[0],
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    tuple(
                        x[0]
                        for x in db.execute(
                            "SELECT claim_version_id FROM draft_claims WHERE draft_version_id=? ORDER BY claim_version_id",
                            (r[0],),
                        )
                    ),
                )
                for r in rows
            )

    def draft(self, version_id: str) -> DraftSnapshot:
        found = next(
            (draft for draft in self.drafts() if draft.draft_version_id == version_id),
            None,
        )
        if found is None:
            raise ValueError("Cần một Draft VIDEO đã lưu và niêm phong.")
        return found

    def media(self, project_id: str) -> tuple[VerifiedMedia, ...]:
        found = []
        with closing(open_readonly(self.db_path)) as db:
            rows = db.execute(
                "SELECT DISTINCT f.file_id,f.asset_id,f.role,f.storage_key,f.sha256,a.media_type FROM asset_files f JOIN assets a ON a.asset_id=f.asset_id JOIN project_references p ON p.asset_id=a.asset_id WHERE p.project_id=? AND p.usage_intent='REUSE_MEDIA' AND a.deleted_at IS NULL AND a.media_type IN ('IMAGE','VIDEO','AUDIO')",
                (project_id,),
            ).fetchall()
            for file_id, asset_id, role, key, digest, kind in rows:
                try:
                    with ExitStack() as handles:
                        hold_publication_media(
                            db,
                            file_id=file_id,
                            expected_digest=digest,
                            data_root=self.root,
                            handles=handles,
                        )
                    found.append(
                        VerifiedMedia(
                            file_id,
                            asset_id,
                            resolve_storage_path(self.root, role, key),
                            digest,
                            kind,
                            True,
                        )
                    )
                except (ValueError, OSError):
                    continue
        return tuple(found)

    def prepare(
        self,
        version_id: str,
        scenes: tuple[Scene, ...],
        preset: RenderPreset,
        subtitles: tuple[Subtitle, ...] = (),
        voiceover_file_id: str | None = None,
    ) -> RenderRequest:
        draft = self.draft(version_id)
        if not scenes or len(scenes) > 40 or any(s.duration_ms <= 0 for s in scenes):
            raise ValueError("Cần 1–40 cảnh có thời lượng dương.")
        if sum(s.duration_ms for s in scenes) > preset.max_duration_ms:
            raise ValueError("Thời lượng vượt preset preview.")
        media_ids = {s.file_id for s in scenes if s.file_id is not None}
        if voiceover_file_id:
            media_ids.add(voiceover_file_id)
        media = tuple(m for m in self.media(draft.project_id) if m.file_id in media_ids)
        if {m.file_id for m in media} != media_ids:
            raise ValueError(
                "Media chưa có tham chiếu REUSE_MEDIA và quyền hợp lệ, hoặc file không còn khớp."
            )
        for scene in scenes:
            if scene.kind != "TITLE" and scene.file_id is None:
                raise ValueError("Cảnh ảnh/clip cần file thật.")
        specs = load_video_components(self.root)
        ensure_data_root(self.root)
        run = uuid4().hex
        output = self.root / "storage" / "derived" / ("render-" + run)
        output.mkdir()
        if output.is_symlink() or output.is_junction():
            raise ValueError("Render output phải là thư mục thật.")
        # Store a real immutable script original; the run's input belongs to it.
        temporary = self.root / "runtime" / ("render-script-" + run + ".txt")
        with temporary.open("x", encoding="utf-8", newline="") as stream:
            stream.write(draft.body_text)
        try:
            script = intake_text_original(temporary, root=self.root)
        finally:
            temporary.unlink(missing_ok=True)
        owner = (
            {**process_identity(_api(), _api().GetCurrentProcess()), "pid": os.getpid()}
            if sys.platform == "win32"
            else None
        )
        payload = {
            "draft_version_id": version_id,
            "owner": owner,
            "scenes": [asdict(s) for s in scenes],
            "subtitles": [asdict(s) for s in subtitles],
            "preset": asdict(preset),
            "voiceover_file_id": voiceover_file_id,
            "media": [
                {"file_id": m.file_id, "sha256": m.sha256, "kind": m.media_type}
                for m in media
            ],
            "components": [
                {"id": s.component_id, "version": s.version, "sha256": s.sha256}
                for s in specs
            ],
        }
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        cache_payload = {key: value for key, value in payload.items() if key != "owner"}
        fingerprint = hashlib.sha256(
            json.dumps(
                cache_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        with closing(_connect_write(self.db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            digest = db.execute(
                "SELECT sha256 FROM asset_files WHERE file_id=?", (script.file_id,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,created_at) VALUES(?,?,?,'DRAFT_VIDEO_RENDER','QUEUED','ffmpeg',?,?)",
                (
                    run,
                    script.asset_id,
                    script.file_id,
                    next(s.version for s in specs if s.component_id == "ffmpeg-exe"),
                    _now(),
                ),
            )
            db.execute(
                "INSERT INTO draft_render_requests VALUES(?,?,?,?,NULL)",
                (run, version_id, serialized, fingerprint),
            )
            db.execute(
                "INSERT INTO draft_render_inputs VALUES(?,0,?,?,'SCRIPT')",
                (run, script.file_id, digest),
            )
            for position, m in enumerate(media, 1):
                usage = {"IMAGE": "IMAGE", "VIDEO": "CLIP", "AUDIO": "AUDIO"}[
                    m.media_type
                ]
                db.execute(
                    "INSERT INTO draft_render_inputs VALUES(?,?,?,?,?)",
                    (run, position, m.file_id, m.sha256, usage),
                )
            db.execute(
                "UPDATE draft_render_requests SET sealed_at=? WHERE render_run_id=?",
                (_now(), run),
            )
            db.commit()
        return RenderRequest(
            run, draft, scenes, output, media, subtitles, voiceover_file_id, preset
        )

    def finish(self, result: RenderResult) -> None:
        if result.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ValueError("Render chưa kết thúc.")
        output = None
        if result.status == "SUCCEEDED":
            if (
                result.output_path is None
                or result.sha256 is None
                or not all(
                    [
                        result.byte_size,
                        result.duration_ms,
                        result.width,
                        result.height,
                        result.tool_version,
                    ]
                )
            ):
                raise ValueError("MP4 chưa có đủ bằng chứng hoàn tất.")
            output = result.output_path.resolve(strict=True)
            expected = (
                self.root / "storage" / "derived" / ("render-" + result.run_id)
            ).resolve()
            if (
                not output.is_relative_to(expected)
                or output.is_symlink()
                or _hash(output) != result.sha256
                or output.stat().st_size != result.byte_size
            ):
                raise ValueError("MP4 output không khớp hoặc ở ngoài thư mục run.")
            with output.open("rb") as stream:
                if stream.read(12)[4:8] != b"ftyp":
                    raise ValueError("Output không phải MP4.")
        with closing(_connect_write(self.db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT q.draft_version_id,r.status FROM draft_render_requests q JOIN processing_runs r ON r.run_id=q.render_run_id WHERE q.render_run_id=? AND q.sealed_at IS NOT NULL",
                (result.run_id,),
            ).fetchone()
            if (
                row is None
                or row[0] != result.draft_version_id
                or row[1] not in {"QUEUED", "RUNNING"}
            ):
                raise ValueError("Render run/version đã đổi hoặc đã kết thúc.")
            db.execute(
                "UPDATE processing_runs SET status=?,finished_at=?,error_code=?,error_message=? WHERE run_id=?",
                (
                    result.status,
                    _now(),
                    "RENDER_FAILED" if result.status == "FAILED" else None,
                    result.error[:800] if result.error else None,
                    result.run_id,
                ),
            )
            if output:
                db.execute(
                    "INSERT INTO draft_render_outputs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        result.run_id,
                        output.relative_to(self.root).as_posix(),
                        result.sha256,
                        result.byte_size,
                        "video/mp4",
                        result.duration_ms,
                        result.width,
                        result.height,
                        result.tool_name,
                        result.tool_version,
                        _now(),
                    ),
                )
            db.commit()

    def render(
        self,
        version_id: str,
        scenes: tuple[Scene, ...],
        preset: RenderPreset,
        progress: Callable[[Progress], None],
        cancelled: Callable[[], bool],
        subtitles: tuple[Subtitle, ...] = (),
        voiceover_file_id: str | None = None,
    ) -> RenderResult:
        cached = self.cached_result(
            version_id, scenes, preset, subtitles, voiceover_file_id
        )
        if cached is not None and not cancelled():
            progress(
                Progress(
                    cached.run_id,
                    "SUCCEEDED",
                    1,
                    1,
                    "Dùng lại MP4 đã kiểm: Draft, inputs, tool và cấu hình không đổi",
                )
            )
            return cached
        request = self.prepare(version_id, scenes, preset, subtitles, voiceover_file_id)
        try:
            from creator_loop.backend.video.renderer import LocalFFmpegRenderer

            specs = load_video_components(self.root)
            with closing(_connect_write(self.db_path)) as db:
                db.execute(
                    "UPDATE processing_runs SET status='RUNNING',started_at=? WHERE run_id=? AND status='QUEUED'",
                    (_now(), request.run_id),
                )
                db.commit()
            result = LocalFFmpegRenderer(self.root, specs).render(
                request, progress, cancelled
            )
        except Exception as exc:
            result = RenderResult(
                request.run_id,
                version_id,
                "CANCELLED" if cancelled() else "FAILED",
                error=str(exc)[:800],
            )
        try:
            self.finish(result)
        except (ValueError, OSError) as exc:
            if result.status != "SUCCEEDED":
                raise
            result = RenderResult(
                request.run_id, version_id, "FAILED", error=str(exc)[:800]
            )
            self.finish(result)
        return result

    def cached_result(
        self,
        version_id: str,
        scenes: tuple[Scene, ...],
        preset: RenderPreset,
        subtitles: tuple[Subtitle, ...],
        voiceover: str | None,
    ) -> RenderResult | None:
        draft = self.draft(version_id)
        ids = {s.file_id for s in scenes if s.file_id}
        if voiceover:
            ids.add(voiceover)
        media = tuple(m for m in self.media(draft.project_id) if m.file_id in ids)
        if {m.file_id for m in media} != ids:
            return None
        specs = load_video_components(self.root)
        expected = {
            "draft_version_id": version_id,
            "scenes": [asdict(s) for s in scenes],
            "subtitles": [asdict(s) for s in subtitles],
            "preset": asdict(preset),
            "voiceover_file_id": voiceover,
            "media": [
                {"file_id": m.file_id, "sha256": m.sha256, "kind": m.media_type}
                for m in media
            ],
            "components": [
                {"id": s.component_id, "version": s.version, "sha256": s.sha256}
                for s in specs
            ],
        }
        with closing(open_readonly(self.db_path)) as db:
            rows = db.execute(
                "SELECT q.render_run_id,q.request_json FROM draft_render_requests q JOIN processing_runs r ON r.run_id=q.render_run_id WHERE q.draft_version_id=? AND r.status='SUCCEEDED' ORDER BY r.rowid DESC",
                (version_id,),
            ).fetchall()
            for run, serialized in rows:
                payload = json.loads(serialized)
                if {k: v for k, v in payload.items() if k != "owner"} != expected:
                    continue
                inputs = db.execute(
                    "SELECT f.role,f.storage_key,i.sha256 FROM draft_render_inputs i JOIN asset_files f ON f.file_id=i.file_id WHERE i.render_run_id=?",
                    (run,),
                ).fetchall()
                try:
                    if any(
                        _hash(resolve_storage_path(self.root, role, key)) != digest
                        for role, key, digest in inputs
                    ):
                        continue
                    return self.result(run)
                except (ValueError, OSError):
                    continue
        return None

    def history(self, version_id: str) -> list[dict[str, object]]:
        with closing(open_readonly(self.db_path)) as db:
            db.row_factory = __import__("sqlite3").Row
            rows = db.execute(
                "SELECT q.render_run_id,q.draft_version_id,r.status,r.tool_version,r.created_at,r.error_message,o.storage_key,o.sha256,o.byte_size,o.duration_ms FROM draft_render_requests q JOIN processing_runs r ON r.run_id=q.render_run_id LEFT JOIN draft_render_outputs o ON o.render_run_id=q.render_run_id WHERE q.draft_version_id=? ORDER BY r.rowid DESC",
                (version_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def completed_output(self, run_id: str) -> Path:
        with closing(open_readonly(self.db_path)) as db:
            row = db.execute(
                "SELECT o.storage_key,o.sha256 FROM draft_render_outputs o JOIN processing_runs r ON r.run_id=o.render_run_id WHERE o.render_run_id=? AND r.status='SUCCEEDED'",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Run chưa có MP4 thành công.")
        path = resolve_storage_path(self.root, "OTHER", row[0])
        if _hash(path) != row[1]:
            raise ValueError("MP4 đã thay đổi; giữ lịch sử và tạo render mới.")
        return path

    def recover_interrupted(self) -> tuple[str, ...]:
        """Mark only runs whose exact owning parent has died; never kill processes."""
        if sys.platform != "win32":
            return ()
        recovered = []
        with closing(_connect_write(self.db_path)) as db:
            rows = db.execute(
                "SELECT q.render_run_id,q.request_json FROM draft_render_requests q JOIN processing_runs r ON r.run_id=q.render_run_id WHERE r.status IN ('QUEUED','RUNNING')"
            ).fetchall()
            for run, payload in rows:
                try:
                    owner = json.loads(payload).get("owner")
                    if not isinstance(owner, dict) or not _identity_dead(owner):
                        continue
                except (ValueError, OSError):
                    continue
                db.execute(
                    "UPDATE processing_runs SET status='CANCELLED',finished_at=?,error_code='PARENT_EXIT',error_message='Phiên render trước đã kết thúc; output chưa được xác nhận.' WHERE run_id=? AND status IN ('QUEUED','RUNNING')",
                    (_now(), run),
                )
                recovered.append(run)
            db.commit()
        return tuple(recovered)

    def result(self, run_id: str) -> RenderResult:
        path = self.completed_output(run_id)
        with closing(open_readonly(self.db_path)) as db:
            row = db.execute(
                "SELECT q.draft_version_id,o.sha256,o.byte_size,o.duration_ms,o.width_px,o.height_px,o.tool_name,o.tool_version FROM draft_render_outputs o JOIN draft_render_requests q ON q.render_run_id=o.render_run_id WHERE o.render_run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("MP4 không còn metadata.")
        return RenderResult(run_id, row[0], "SUCCEEDED", path, *row[1:])
