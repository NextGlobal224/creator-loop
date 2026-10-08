"""Opt-in real compiled UI workflow using new private fixture data only."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from PySide6.QtCore import Qt, QTimer
from PySide6.QtMultimedia import QAudioBufferOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
    QTableWidgetItem,
)

from creator_loop.app_lock import AppDataLock
from creator_loop.audio_evidence_ui import AudioEvidenceDialog, AudioRangeView
from creator_loop.claim_ui import ClaimDialog
from creator_loop.creator_ui import CreatorDialog
from creator_loop.database import initialize, open_readonly
from creator_loop.evidence_review_ui import EvidenceReviewDialog
from creator_loop.image_evidence_ui import ImageEvidenceDialog, ImageRegionView
from creator_loop.library_ui import LibraryWindow
from creator_loop.observation_ui import ObservationDialog
from creator_loop.observations import list_observations
from creator_loop.paths import ensure_data_root
from creator_loop.project_ui import ProjectDialog
from creator_loop.publication import list_approvals, list_posts, package_snapshot
from creator_loop.publication_ui import PublicationDialog
from creator_loop.selection_ui import SelectionDialog
from creator_loop.selections import list_selections
from creator_loop.source_ui import SourceDialog
from creator_loop.text_evidence_ui import TextEvidenceDialog
from creator_loop.video_evidence_ui import VideoEvidenceDialog, VideoRangeView
from creator_loop.whole_evidence_ui import WholeEvidenceDialog, WholeEvidenceView
from creator_loop.windows_paths import file_io_path


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


@contextmanager
def _replace(owner: Any, name: str, replacement: Any) -> Iterator[Any]:
    original = getattr(owner, name)
    setattr(owner, name, replacement)
    try:
        yield replacement
    finally:
        setattr(owner, name, original)


class _Notices:
    def __init__(self) -> None:
        self.called = False
        self.last_args: tuple[Any, ...] = ()

    def __call__(self, *args: Any, **kwargs: Any) -> QMessageBox.StandardButton:
        self.called = True
        self.last_args = args
        return QMessageBox.StandardButton.Ok


def _validate_work_root(work_root: Path) -> None:
    if os.name != "nt":
        raise ValueError("Product UI probe requires Windows")
    if not work_root.is_absolute() or not work_root.is_dir():
        raise ValueError("An existing absolute fixture work root is required")
    for p in (work_root, *work_root.parents):
        if p.is_symlink() or p.is_junction():
            raise ValueError("Linked fixture root is forbidden")
    if (
        (work_root / "data").exists()
        or (work_root / "data").is_symlink()
        or (work_root / "data").is_junction()
    ):
        raise FileExistsError("Refuse existing data root; keep previous probe evidence")
    inputs = work_root / "inputs"
    if not inputs.is_dir() or inputs.is_symlink() or inputs.is_junction():
        raise ValueError("Real private fixture input directory is required")
    for name in ("Original.txt", "Original.png", "Original.mp4"):
        p = inputs / name
        if (
            not p.is_file()
            or p.is_symlink()
            or p.is_junction()
            or not 0 < p.stat().st_size <= 1024 * 1024
        ):
            raise ValueError("Missing/linked/oversized fixture input")


def run_product_flow_smoke(work_root: Path) -> dict[str, Any]:
    scenario = _Scenario()
    try:
        return scenario.run(work_root)
    finally:
        if hasattr(scenario, "stack"):
            scenario.stack.close()


class _Scenario:
    def _until(self, predicate, label):
        deadline = time.monotonic() + 20
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        _require(predicate(), f"Product workflow timeout: {label}")

    @contextmanager
    def _modal(self, cls, input_action):
        """Supply inputs inside the actual modal event loop; propagate slot errors."""
        original = cls.exec
        failures: list[Exception] = []

        def execute(dialog):
            self.addCleanup(dialog.close)

            def provide_input():
                try:
                    input_action(dialog)
                except Exception as exc:
                    failures.append(exc)
                    dialog.reject()

            QTimer.singleShot(50, provide_input)
            return original(dialog)

        with _replace(cls, "exec", execute):
            yield
        if failures:
            raise RuntimeError(f"{cls.__name__} input/preview failed") from failures[0]

    def _accept(self, dialog):
        button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        _require(button.isEnabled(), "Valid fixture inputs did not enable confirmation")
        button.click()

    def _select_evidence(self, library, version_id):
        for row in range(library.evidence_table.rowCount()):
            if (
                library.evidence_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                == version_id
            ):
                library.evidence_table.selectRow(row)
                return row
        raise RuntimeError("Exact Evidence Version missing from Library")

    def _new_version(self, before):
        with closing(open_readonly(self.path)) as db:
            after = {
                r[0]
                for r in db.execute("SELECT evidence_version_id FROM evidence_versions")
            }
        new = after - before
        _require(len(new) == 1, "Evidence action must append exactly one version")
        return new.pop()

    def _versions(self):
        with closing(open_readonly(self.path)) as db:
            return {
                r[0]
                for r in db.execute("SELECT evidence_version_id FROM evidence_versions")
            }

    def _review(self, library, version_id):
        self._select_evidence(library, version_id)
        with self._modal(EvidenceReviewDialog, self._accept):
            library.choose_evidence_review()
            self._wait(library)
        row = self._select_evidence(library, version_id)
        _require(
            library.evidence_table.item(row, 3).text() == "ACCEPT",
            "Exact Evidence not accepted",
        )

    def _play(self, segment, *, whole=False, audio_only=False):
        """Observe real child pixels/PCM, never substitute player/decoder results."""
        player = segment.player
        frames, buffers, errors = [], [], []
        if player.videoSink() is not None:
            player.videoSink().videoFrameChanged.connect(
                lambda frame: (
                    frames.append(frame.startTime()) if frame.isValid() else None
                )
            )
        output = QAudioBufferOutput(player)
        player.setAudioBufferOutput(output)
        output.audioBufferReceived.connect(
            lambda buffer: (
                buffers.append(
                    (buffer.startTime(), buffer.startTime() + buffer.duration())
                )
                if buffer.isValid()
                else None
            )
        )
        player.errorOccurred.connect(lambda _code, message: errors.append(message))
        if player.process is None:
            segment.play_button.click()
        self._until(
            lambda: (
                bool(errors)
                or (
                    player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia
                    if whole
                    else player.playbackState()
                    == QMediaPlayer.PlaybackState.PausedState
                )
                and (audio_only or bool(frames))
                and (not (whole or audio_only) or bool(buffers))
            ),
            "actual selected playback",
        )
        _require(not errors, f"Actual playback error: {errors}")
        start, end = segment.start_ms, segment.end_ms
        _require(audio_only or bool(frames), "Video player delivered no actual frames")
        if whole or audio_only:
            _require(bool(buffers), "Audio player delivered no actual PCM")
        _require(
            all(start * 1000 <= pts < end * 1000 for pts in frames),
            "Frame escaped selected range",
        )
        _require(
            all(
                start * 1000 <= begin < finish <= end * 1000
                for begin, finish in buffers
            ),
            "PCM escaped selected range",
        )
        segment.stop()
        self._until(
            lambda: (
                player.process is None
                and player.pipe is None
                and player.held is None
                and player.workspace is None
            ),
            "owned playback resources retired",
        )
        _require(player.source().isEmpty(), "Closed player kept source")
        self.playback.append(
            {
                "start_ms": start,
                "end_ms": end,
                "frames": len(frames),
                "pcm_buffers": len(buffers),
                "whole": whole,
                "audio_only": audio_only,
            }
        )

    def _media_evidence(self, library):
        self.playback = []
        histories = {}
        region = {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5}

        def image_input(dialog):
            chosen = (
                {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}
                if dialog.correction
                else region
            )
            if dialog.correction:
                _require(
                    dialog.region() == region, "Correction lost saved image locator"
                )
                dialog.reason.setText("Đối chiếu lại vùng bên phải")
            for key, value in chosen.items():
                dialog.coords[key].setValue(value)
            dialog.content.setText(
                "Vùng bên phải" if dialog.correction else "Vùng giữa ảnh"
            )
            self._accept(dialog)

        def time_input(dialog):
            if dialog.correction:
                _require(
                    (dialog.start.value(), dialog.end.value()) == (203, 607),
                    "Correction lost saved time locator",
                )
                dialog.reason.setText("Đối chiếu đoạn sau")
            dialog.start.setValue(701 if dialog.correction else 203)
            dialog.end.setValue(901 if dialog.correction else 607)
            dialog.content.setText("Đoạn sau" if dialog.correction else "Đoạn đầu")
            if isinstance(dialog, AudioEvidenceDialog):
                if dialog.correction:
                    _require(
                        dialog.kind.currentData() == "OTHER"
                        and not dialog.kind.isEnabled(),
                        "Correction changed audio type",
                    )
                else:
                    dialog.kind.setCurrentIndex(dialog.kind.findData("OTHER"))
            self._accept(dialog)

        for name, kind, cls, create, correct in (
            (
                "image",
                "IMAGE",
                ImageEvidenceDialog,
                library.choose_image_evidence,
                library.correct_selected_image_evidence,
            ),
            (
                "video",
                "VIDEO",
                VideoEvidenceDialog,
                library.choose_video_evidence,
                library.correct_selected_video_evidence,
            ),
            (
                "audio",
                "VIDEO",
                AudioEvidenceDialog,
                library.choose_audio_evidence,
                library.correct_selected_audio_evidence,
            ),
        ):
            self._select_original(library, kind)
            before = self._versions()
            with self._modal(cls, image_input if name == "image" else time_input):
                create()
                self._wait(library)
            old_id = self._new_version(before)
            self._select_evidence(library, old_id)
            before = self._versions()
            with self._modal(cls, image_input if name == "image" else time_input):
                correct()
                self._wait(library)
            new_id = self._new_version(before)
            self._review(library, new_id)
            histories[name] = [old_id, new_id]

        whole_versions = {}
        for kind in ("TEXT", "IMAGE", "VIDEO"):
            self._select_original(library, kind)
            before = self._versions()

            def whole_input(dialog):
                _require(
                    not dialog.confirmed.isChecked(),
                    "Whole-source consent was preselected",
                )
                _require(
                    not dialog.buttons.button(
                        QDialogButtonBox.StandardButton.Ok
                    ).isEnabled(),
                    "Whole-source consent not required",
                )
                if kind == "TEXT":
                    _require(
                        dialog.content.isReadOnly()
                        and dialog.content.toPlainText() == "Café ở Huế",
                        "Whole TEXT snapshot changed",
                    )
                else:
                    dialog.content.setPlainText(f"Toàn nguồn {kind} mẫu")
                dialog.confirmed.setChecked(True)
                self._accept(dialog)

            with self._modal(WholeEvidenceDialog, whole_input):
                library.choose_whole_evidence("load")
                self._wait(library)
            version_id = self._new_version(before)
            self._review(library, version_id)
            whole_versions[kind] = version_id
        return {"histories": histories, "whole_versions": whole_versions}

    def _reopen_media(self, library, media):
        for name, cls, reopen in (
            ("image", ImageRegionView, library.reopen_selected_image_evidence),
            ("video", VideoRangeView, library.reopen_selected_video_evidence),
            ("audio", AudioRangeView, library.reopen_selected_audio_evidence),
        ):
            for index, version_id in enumerate(media["histories"][name]):
                self._select_evidence(library, version_id)

                def inspect(view):
                    if name == "image":
                        pixmap = view.region_view.pixmap()
                        _require(
                            not pixmap.isNull(), "Image view did not show verified crop"
                        )
                        first, last = (
                            pixmap.toImage().pixelColor(0, 0),
                            pixmap.toImage().pixelColor(pixmap.width() - 1, 0),
                        )
                        _require(
                            last.red() > last.blue(),
                            "Image crop lost right-hand pixels",
                        )
                        _require(
                            (first.red() > first.blue())
                            if index
                            else (first.blue() > first.red()),
                            "Old/new image crop pixels incorrect",
                        )
                    else:
                        expected = (701, 901) if index else (203, 607)
                        _require(
                            (view.segment.start_ms, view.segment.end_ms) == expected,
                            "Reopened time range changed",
                        )
                        self._play(view.segment, audio_only=name == "audio")
                    view.reject()

                with self._modal(cls, inspect):
                    reopen()
                    self._wait(library)
        for kind, version_id in media["whole_versions"].items():
            self._select_evidence(library, version_id)

            def inspect_whole(view):
                if kind == "TEXT":
                    _require(
                        view.preview.text.isReadOnly()
                        and view.preview.text.toPlainText() == "Café ở Huế",
                        "Whole TEXT reopen changed",
                    )
                elif kind == "IMAGE":
                    pixmap = view.preview.image.pixmap()
                    _require(
                        (pixmap.width(), pixmap.height()) == (12, 8),
                        "Whole image not shown at 1:1",
                    )
                else:
                    _require(
                        view.preview.audio is not None, "Whole VIDEO lost audio output"
                    )
                    self._play(view.preview.segment, whole=True)
                view.reject()

            with self._modal(WholeEvidenceView, inspect_whole):
                library.choose_whole_evidence("reopen")
                self._wait(library)

    def _wait(self, window):
        deadline = time.monotonic() + 20
        while window._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        _require(
            window._worker is None, "Product workflow check failed at scenario line 53"
        )
        self.app.processEvents()
        _require(
            not self.warning.called, "Product workflow check failed at scenario line 55"
        )

    def _dialog(self, cls, *args):
        window = cls(self.root, *args)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        return window

    def _select_original(self, library, kind):
        matches = [
            row
            for row in range(library.table.rowCount())
            if library.table.item(row, 0).text() == kind
        ]
        _require(len(matches) == 1, "Product workflow check failed at scenario line 68")
        library.table.selectRow(matches[0])
        return library.table.item(matches[0], 0).data(Qt.ItemDataRole.UserRole)

    def run(self, work_root: Path):
        self.app = QApplication.instance() or QApplication([])
        self.root = work_root / "data"
        _validate_work_root(work_root)
        self.root.mkdir()
        ensure_data_root(self.root)
        self.path = self.root / "creator_loop.sqlite3"
        self.warning = _Notices()
        self.stack = ExitStack()
        self.addCleanup = self.stack.callback
        self.stack.enter_context(_replace(QMessageBox, "warning", self.warning))
        with AppDataLock(self.root):
            initialize(self.path)
            library = self._dialog(LibraryWindow)
            library.show()
            text = work_root / "inputs" / "Original.txt"
            image_path = work_root / "inputs" / "Original.png"
            video = work_root / "inputs" / "Original.mp4"
            for kind, source in (
                ("TEXT", text),
                ("IMAGE", image_path),
                ("VIDEO", video),
            ):
                with _replace(
                    QFileDialog, "getOpenFileName", lambda *a, **k: (str(source), "")
                ):
                    library.choose_original(kind, "Tất cả tệp (*)")
                self._wait(library)
            _require(
                library.table.rowCount() == 3,
                "Product workflow check failed at scenario line 104",
            )
            image_id = self._select_original(library, "IMAGE")

            def owned_image(dialog):
                dialog.platform.setText("LOCAL")
                dialog.rights.setCurrentText("OWNED")
                dialog.relationship.setCurrentText("ORIGIN")
                return SourceDialog.DialogCode.Accepted

            with _replace(SourceDialog, "exec", owned_image):
                library.choose_source()
                self._wait(library)
            self._select_original(library, "TEXT")

            def excerpt(dialog):
                dialog.start.setValue(0)
                dialog.end.setValue(len("Café ở Huế"))
                return TextEvidenceDialog.DialogCode.Accepted

            with _replace(TextEvidenceDialog, "exec", excerpt):
                library.choose_text_evidence()
                self._wait(library)
            _require(
                library.evidence_table.item(0, 1).text() == "Café ở Huế",
                "Product workflow check failed at scenario line 126",
            )
            evidence_id = library.evidence_table.item(0, 0).data(
                Qt.ItemDataRole.UserRole
            )
            library.evidence_table.selectRow(0)
            with _replace(QMessageBox, "information", _Notices()) as displayed:
                library.reopen_selected_text_evidence()
                self._wait(library)
            _require(
                displayed.last_args[2] == "Café ở Huế",
                "Product workflow check failed at scenario line 134",
            )
            with _replace(
                EvidenceReviewDialog, "exec", lambda d: d.DialogCode.Accepted
            ):
                library.choose_evidence_review()
                self._wait(library)
            _require(
                library.evidence_table.item(0, 3).text() == "ACCEPT",
                "Product workflow check failed at scenario line 140",
            )
            media = self._media_evidence(library)
            claim = self._dialog(ClaimDialog)
            claim.statement.setPlainText("Café ở Huế")
            claim.evidence.setCurrentIndex(claim.evidence.findData(evidence_id))
            claim.add_button.click()
            claim.save_button.click()
            self._wait(claim)
            claim_id = claim.selected_version_id()
            _require(
                claim_id is not None,
                "Product workflow check failed at scenario line 149",
            )
            claim.review_action.setCurrentIndex(claim.review_action.findData("ACCEPT"))
            claim.review_button.click()
            self._wait(claim)
            _require(
                "ACCEPT" in claim.summary.text(),
                "Product workflow check failed at scenario line 153",
            )
            project = self._dialog(ProjectDialog)
            project.title.setText("Chuyện Huế")
            project.create_button.click()
            self._wait(project)
            project_id = project.selected_project_id()
            _require(
                project_id is not None,
                "Product workflow check failed at scenario line 160",
            )
            project.target_type.setCurrentIndex(
                project.target_type.findData("claim_version_id")
            )
            project.target.setCurrentIndex(project.target.findData(claim_id))
            project.usage.setCurrentIndex(project.usage.findData("RESEARCH"))
            project.add_button.click()
            self._wait(project)
            creator = self._dialog(CreatorDialog)
            creator.project.setCurrentIndex(creator.project.findData(project_id))
            drafts = []
            for suffix in ("A", "B"):
                creator.new_button.click()
                creator.body.setPlainText(f"Café ở Huế — phương án {suffix}")
                creator.claim.setCurrentIndex(creator.claim.findData(claim_id))
                creator.add_claim_button.click()
                creator.start.setValue(0)
                creator.end.setValue(len("Café ở Huế"))
                creator.assertion_claim.setCurrentIndex(
                    creator.assertion_claim.findData(claim_id)
                )
                creator.assertion_state.setCurrentText("SUPPORTED")
                creator.add_assertion_button.click()
                _require(
                    creator.save_button.isEnabled(),
                    "Product workflow check failed at scenario line 184",
                )
                creator.save_button.click()
                self._wait(creator)
                drafts.append(creator.selected_version_id())
                creator.review_action.setCurrentIndex(
                    creator.review_action.findData("ACCEPT")
                )
                creator.review_button.click()
                self._wait(creator)
            _require(
                drafts[1] is not None,
                "Product workflow check failed at scenario line 193",
            )
            _require(
                drafts[0] != drafts[1],
                "Product workflow check failed at scenario line 194",
            )
            selection = self._dialog(SelectionDialog)
            selection.project.setCurrentIndex(selection.project.findData(project_id))
            for row in range(selection.candidates.count()):
                item = selection.candidates.item(row)
                if item.data(Qt.ItemDataRole.UserRole) in drafts:
                    item.setCheckState(Qt.CheckState.Checked)
            selection.selected.setCurrentIndex(selection.selected.findData(drafts[1]))
            selection.save_button.click()
            self._wait(selection)
            publication = self._dialog(PublicationDialog)
            publication.project.setCurrentIndex(
                publication.project.findData(project_id)
            )
            publication.draft.setCurrentIndex(publication.draft.findData(drafts[1]))
            publication.file.setCurrentIndex(publication.file.findData(image_id))
            publication.add_button.click()
            _require(
                publication.media.count() == 1,
                "Product workflow check failed at scenario line 213",
            )
            publication.create_button.click()
            self._wait(publication)
            package_id = publication.selected_package_id()
            _require(
                package_id is not None,
                "Product workflow check failed at scenario line 217",
            )
            publication.check_button.click()
            self._wait(publication)
            _require(
                not publication.review_confirmed.isChecked(),
                "Product workflow check failed at scenario line 220",
            )
            _require(
                not publication.approve_button.isEnabled(),
                "Product workflow check failed at scenario line 221",
            )
            publication.actor.setText("reviewer Huế")
            publication.review_confirmed.setChecked(True)
            _require(
                publication.approve_button.isEnabled(),
                "Product workflow check failed at scenario line 224",
            )
            publication.approve_button.click()
            self._wait(publication)
            publication.check_button.click()
            self._wait(publication)
            publication.prepare_button.click()
            self._wait(publication)
            _require(
                publication.selected_post().status == "PENDING",
                "Product workflow check failed at scenario line 234",
            )
            publication.check_button.click()
            self._wait(publication)
            publication.published_at.setText("2026-10-05T08:00:00+07:00")
            _require(
                not publication.publish_button.isEnabled(),
                "Product workflow check failed at scenario line 238",
            )
            publication.post_confirmed.setChecked(True)
            publication.publish_button.click()
            self._wait(publication)
            post_id = publication.selected_post().post_id
            _require(
                publication.selected_post().status == "PUBLISHED",
                "Product workflow check failed at scenario line 243",
            )
            observation = self._dialog(ObservationDialog, post_id)
            observation.observed_at.setText("2026-10-05T09:00:00+07:00")
            for key, value in (("views", "0"), ("reach", "")):
                observation.add_button.click()
                row = observation.metrics.rowCount() - 1
                for col, value in ((0, key), (2, value), (4, "fixture-v1")):
                    observation.metrics.setItem(row, col, QTableWidgetItem(value))
            observation.save_button.click()
            self._wait(observation)
            _require(
                not observation.save_button.isEnabled(),
                "Product workflow check failed at scenario line 254",
            )
            publication.reason.setText("Rút duyệt sau khi đã đăng; giữ lịch sử")
            publication.revoke_button.click()
            self._wait(publication)
            publication.check_button.click()
            self._wait(publication)
            _require(
                not publication.prepare_button.isEnabled(),
                "Product workflow check failed at scenario line 260",
            )
            with closing(open_readonly(self.path)) as db:
                retained = {
                    key: file_io_path(self.root / key).read_bytes()
                    for (key,) in db.execute("SELECT storage_key FROM asset_files")
                }
                snapshot = package_snapshot(db, package_id)
                _require(
                    snapshot.draft_version_id == drafts[1],
                    "Product workflow check failed at scenario line 268",
                )
                _require(
                    snapshot.items[0][2] == "Café ở Huế — phương án B",
                    "Product workflow check failed at scenario line 269",
                )
                decision = list_selections(db, project_id=project_id)[0]
                _require(
                    set(decision.candidate_version_ids) == set(drafts),
                    "Product workflow check failed at scenario line 271",
                )
                _require(
                    decision.selected_draft_version_id == drafts[1],
                    "Product workflow check failed at scenario line 272",
                )
                measurement = list_observations(db, post_id)[0]
                _require(
                    measurement.value_for("views") == 0,
                    "Product workflow check failed at scenario line 274",
                )
                _require(
                    measurement.value_for("reach") is None,
                    "Product workflow check failed at scenario line 275",
                )
                _require(
                    [a.decision for a in list_approvals(db, package_id)]
                    == ["APPROVED", "REVOKED"],
                    "Product workflow check failed at scenario line 276",
                )
                _require(
                    list_posts(db, package_id)[0].status == "PUBLISHED",
                    "Product workflow check failed at scenario line 280",
                )
                _require(
                    db.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "Product workflow check failed at scenario line 281",
                )
                _require(
                    db.execute("PRAGMA foreign_key_check").fetchall() == [],
                    "Product workflow check failed at scenario line 284",
                )
            for window in (
                observation,
                publication,
                selection,
                creator,
                project,
                claim,
                library,
            ):
                _require(
                    window.close(), "Product workflow check failed at scenario line 294"
                )
            reopened = self._dialog(LibraryWindow)
            _require(
                reopened.table.rowCount() == 3,
                "Product workflow check failed at scenario line 296",
            )
            row = self._select_evidence(reopened, evidence_id)
            _require(
                reopened.evidence_table.item(row, 3).text() == "ACCEPT",
                "Reopened TEXT review changed",
            )
            self._reopen_media(reopened, media)
            read_package = self._dialog(PublicationDialog)
            read_package.project.setCurrentIndex(
                read_package.project.findData(project_id)
            )
            read_package.packages.setCurrentIndex(
                read_package.packages.findData(package_id)
            )
            _require(
                read_package.posts.rowCount() == 1,
                "Product workflow check failed at scenario line 305",
            )
            read_package.posts.selectRow(0)
            _require(
                read_package.selected_post().post_id == post_id,
                "Product workflow check failed at scenario line 307",
            )
            read_observation = self._dialog(ObservationDialog, post_id)
            read_observation.history.setCurrentIndex(
                read_observation.history.findData(measurement.observation_id)
            )
            _require(
                read_observation.metrics.rowCount() == 1,
                "Product workflow check failed at scenario line 312",
            )
            _require(
                float(read_observation.metrics.item(0, 2).text()) == 0,
                "Product workflow check failed at scenario line 313",
            )
            _require(
                read_observation.metrics.item(0, 5).text() == "0",
                "Product workflow check failed at scenario line 314",
            )
            _require(
                not read_observation.save_button.isEnabled(),
                "Product workflow check failed at scenario line 315",
            )
            for key, content in retained.items():
                _require(
                    hashlib.sha256(file_io_path(self.root / key).read_bytes()).digest()
                    == hashlib.sha256(content).digest(),
                    "Product workflow check failed at scenario line 317",
                )
            for window in (read_observation, read_package, reopened):
                _require(
                    window.close(), "Product workflow check failed at scenario line 322"
                )
            self.app.processEvents()
        return {
            "scope": "programmatic real Qt workflow; not human/hardware/release acceptance",
            "frozen": bool(getattr(sys, "frozen", False)),
            "data_root": str(self.root),
            "package_id": package_id,
            "post_id": post_id,
            "original_count": 3,
            "reopened": True,
            "revoked_history_retained": True,
            "zero_distinct_from_missing": True,
            "media_evidence": media,
            "playback": self.playback,
        }
