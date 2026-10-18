"""Explicit native B QA over a caller-created private copy; never calls a model."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication

from creator_loop.backend.analysis.workers_creator import WorkersCreator
from creator_loop.backend.creator.research_pipeline import ResearchPipeline
from creator_loop.backend.runtime.app_lock import AppDataLock
from creator_loop.backend.runtime.database import initialize, open_readonly, validate
from creator_loop.frontend.creator_ui import CreatorDialog
from creator_loop.frontend.research_creator_ui import ResearchCreatorDialog


def run_b_release_smoke(work: Path) -> dict[str, Any]:
    """Inspect actual saved outputs/UI; compatible live generation is separate evidence.

    The opt-in caller must prepare a new, isolated QA copy and bind its DB digest.
    This probe may append a manual choice-only version to that copy, never the
    original data. It does not mock successful model output or grant review.
    """
    if (
        sys.platform != "win32"
        or not work.is_absolute()
        or not re.fullmatch(r"B-release-QA-[0-9a-f]{8}", work.name)
    ):
        raise ValueError("Explicit isolated Windows B QA root required")
    for path in (work, *work.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Linked B QA root refused")
    marker = work / "B-QA.json"
    if marker.is_symlink() or not 0 < marker.stat().st_size <= 4096:
        raise ValueError("Bound B QA input marker required")
    config = json.loads(marker.read_text("utf-8"))
    if (
        set(config) != {"format", "db_sha256", "versions"}
        or type(config["format"]) is not int
        or config["format"] != 1
    ):
        raise ValueError("Unknown B QA input fields")
    if set(config["versions"]) != {"TEXT", "IMAGE_CAPTION", "VIDEO"}:
        raise ValueError("All B output formats required")
    data = work / "data"
    database = data / "creator_loop.sqlite3"
    for path in (data, database):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Linked B QA data refused")
    if hashlib.sha256(database.read_bytes()).hexdigest() != config["db_sha256"]:
        raise ValueError("B QA database differs from supplied copy")
    if (work / "receipt.json").exists():
        raise FileExistsError("Preserve prior B QA receipt")
    app = QApplication.instance() or QApplication([])
    receipt: dict[str, Any] = {
        "model_calls": 0,
        "network_requests": 0,
        "canonical_data_modified": False,
        "outputs": [],
    }
    with AppDataLock(data):
        initialize(database)
        pipeline = ResearchPipeline(data)
        with closing(open_readonly(database)) as db:
            validate(db)
            protected = {
                name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                for name in ("review_events", "approvals", "posts", "selection_events")
            }
        dialog = ResearchCreatorDialog(data)
        dialog.show()
        app.processEvents()
        try:
            for kind, version in config["versions"].items():
                if not isinstance(version, str) or not re.fullmatch(
                    r"[0-9a-f]{32}", version
                ):
                    raise ValueError("Exact saved B Version required")
                snapshot = pipeline.draft(version)
                if snapshot.format != kind:
                    raise ValueError("B format/version mismatch")
                with closing(open_readonly(database)) as db:
                    workflow = db.execute(
                        "SELECT workflow_id FROM creator_workflow_drafts WHERE draft_version_id=?",
                        (version,),
                    ).fetchone()
                if workflow is None:
                    raise ValueError("B Version lacks actual workflow lineage")
                dialog.project.setCurrentIndex(
                    dialog.project.findData(snapshot.project_id)
                )
                dialog.history.setCurrentIndex(dialog.history.findData(workflow[0]))
                app.processEvents()
                if dialog.workflow_id != workflow[0] or not dialog.body.toPlainText():
                    raise RuntimeError("Native B history failed to open")
                latest = pipeline.draft(pipeline.load(workflow[0])["drafts"][0])
                if dialog.body.toPlainText() != latest.body_text:
                    raise RuntimeError(
                        "B workflow history does not show its actual latest version"
                    )
                exact_dialog = CreatorDialog(data)
                try:
                    exact_dialog.project.setCurrentIndex(
                        exact_dialog.project.findData(snapshot.project_id)
                    )
                    if (
                        not exact_dialog.reload(version)
                        or exact_dialog.selected_version_id() != version
                    ):
                        raise RuntimeError("Creator exact Version UI failed to reopen")
                    if exact_dialog._raw_body != snapshot.body_text:
                        raise RuntimeError(
                            "Creator exact body differs from saved Version"
                        )
                finally:
                    exact_dialog.close()
                dialog.mode.setCurrentIndex(dialog.mode.findData("AUTOMATIC"))
                if not dialog.body.isReadOnly() or not dialog.auto_button.isEnabled():
                    raise RuntimeError("Automatic B controls incorrect")
                dialog.mode.setCurrentIndex(dialog.mode.findData("MANUAL"))
                if dialog.body.isReadOnly() or not dialog.draft_button.isEnabled():
                    raise RuntimeError("Manual B controls incorrect")
                if kind == "IMAGE_CAPTION":
                    if (
                        not snapshot.image_file_id
                        or not snapshot.image_usage_note
                        or not snapshot.image_provenance
                    ):
                        raise RuntimeError(
                            "Actual image association/provenance required"
                        )
                    if dialog.image_choice.currentData() != snapshot.image_file_id:
                        raise RuntimeError("Native image selection lost on reopen")
                app.processEvents()
                if not dialog.grab().save(str(work / (kind + "-native.png"))):
                    raise RuntimeError("B native screenshot failed")
                receipt["outputs"].append(
                    {
                        "format": kind,
                        "version": version,
                        "body_sha256": hashlib.sha256(
                            snapshot.body_text.encode()
                        ).hexdigest(),
                        "direction": snapshot.direction,
                        "image_sha256": snapshot.image_sha256,
                        "native_history": True,
                        "workflow_latest_version": latest.draft_version_id,
                        "exact_version_ui": True,
                        "automatic_manual_controls": True,
                    }
                )
            # UI worker performs a real explicit manual save; same prose, new choice.
            prior = pipeline.load(dialog.workflow_id)["drafts"][0]
            before = pipeline.draft(prior)
            direction = (
                before.direction or dialog.direction.toPlainText()
            ) + " — lựa chọn QA riêng"
            dialog.direction.setPlainText(direction)
            dialog.body.setPlainText(before.body_text)
            dialog.body.document().setModified(True)
            dialog.draft_button.click()
            deadline = time.monotonic() + 30
            while dialog.worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            if dialog.worker is not None or dialog.error:
                raise RuntimeError(
                    "Native B save failed or exceeded deadline: " + dialog.error
                )
            after = pipeline.draft(pipeline.load(dialog.workflow_id)["drafts"][0])
            if (
                after.body_text != before.body_text
                or after.direction != direction
                or after.version_no != before.version_no + 1
            ):
                raise RuntimeError("Manual B choice/version persistence failed")
            receipt["native_manual_save"] = {
                "before": prior,
                "after": after.draft_version_id,
                "direction_persisted": True,
                "same_body": True,
            }
        finally:
            if dialog.worker is not None:
                dialog.cancel()
                deadline = time.monotonic() + 15
                while dialog.worker is not None and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
            dialog.close()
            app.processEvents()
        if dialog.worker is not None:
            raise RuntimeError("B UI worker did not exit")
        with closing(open_readonly(database)) as db:
            validate(db)
            if protected != {
                name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                for name in protected
            }:
                raise RuntimeError("QA changed human review/publication state")
        package = (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[3]
        )
        template = package / "examples/B/creator-cloud.json"
        if not template.exists() and not getattr(sys, "frozen", False):
            template = package / "docs/examples/B/creator-cloud.json"
        provider = WorkersCreator(data, template)
        receipt["package_cloud_config"] = {
            "model": provider.model.component_id,
            "worker_version": provider.worker_version,
            "config_sha256": hashlib.sha256(template.read_bytes()).hexdigest(),
            "inference_executed": False,
        }
        receipt.update(
            integrity_fk_schema="PASS",
            manual_review_unchanged=protected,
            normal_close=True,
            live_generation_evidence="Separate compatible actual run required; this native probe makes no model calls",
        )
    (work / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return receipt
