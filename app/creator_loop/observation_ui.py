"""Human reported measurements and immutable history for one exact Post."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from creator_loop.claims import _timestamp
from creator_loop.database import open_readonly
from creator_loop.observations import MetricInput, Observation, list_observations
from creator_loop.packages import PublicationRepository


class ObservationWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        root: Path,
        post_id: str,
        observed_at: str,
        metrics: tuple[MetricInput, ...],
    ) -> None:
        super().__init__()
        self.root, self.post_id = root, post_id
        self.observed_at, self.metrics = observed_at, metrics

    def run(self) -> None:
        try:
            result = PublicationRepository(
                self.root / "creator_loop.sqlite3"
            ).record_manual_observation(
                post_id=self.post_id,
                observed_at=self.observed_at,
                metrics=self.metrics,
            )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class ObservationDialog(QDialog):
    def __init__(self, root: Path, post_id: str) -> None:
        super().__init__()
        self.root, self.post_id = root, post_id
        self._worker: ObservationWorker | None = None
        self._result: Observation | None = None
        self._failure: str | None = None
        self._loaded = False
        self._eligible = False
        self._history: dict[str, Observation] = {}
        self.setWindowTitle("Observation / Số liệu Post")
        self.resize(920, 570)
        layout = QVBoxLayout(self)
        self.post_label = QLabel()
        self.post_label.setWordWrap(True)
        layout.addWidget(self.post_label)
        note = QLabel(
            "Nhập số đo thủ công đã quan sát. MANUAL / run NULL; không gọi API. "
            "Thiếu số để trống = NULL, không phải 0. Definition do người nhập cung cấp."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        header = QHBoxLayout()
        self.history = QComboBox()
        self.history.setMinimumContentsLength(20)
        self.history.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.reload_button = QPushButton("Đọc lại")
        self.new_button = QPushButton("Lần đo mới")
        header.addWidget(self.history, 1)
        header.addWidget(self.new_button)
        header.addWidget(self.reload_button)
        layout.addLayout(header)
        self.observed_at = QLineEdit(_timestamp())
        layout.addWidget(
            QLabel("Thời điểm đo ISO 8601 có timezone (không phải ngày đăng)")
        )
        layout.addWidget(self.observed_at)
        self.provenance = QLabel()
        self.provenance.setWordWrap(True)
        layout.addWidget(self.provenance)
        self.metrics = QTableWidget(0, 6)
        self.metrics.setHorizontalHeaderLabels(
            (
                "Metric key",
                "Scope",
                "Số / trống=NULL",
                "Unit",
                "Definition version",
                "Raw value",
            )
        )
        layout.addWidget(self.metrics, 1)
        actions = QHBoxLayout()
        self.add_button = QPushButton("Thêm metric")
        self.remove_button = QPushButton("Bỏ metric đang chọn")
        self.save_button = QPushButton("Lưu lần đo mới")
        for button in (self.add_button, self.remove_button, self.save_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        layout.addWidget(self.buttons)
        self.history.currentIndexChanged.connect(self._select)
        self.reload_button.clicked.connect(lambda: self.reload())
        self.new_button.clicked.connect(self.new_observation)
        self.add_button.clicked.connect(self.add_metric)
        self.remove_button.clicked.connect(self.remove_metric)
        self.save_button.clicked.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        self.reload()

    def reload(self, selected: str | None = None) -> bool:
        if self._worker is not None:
            return False
        try:
            with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                db.execute("BEGIN")
                post = db.execute(
                    "SELECT platform,status,published_at FROM posts WHERE post_id=?",
                    (self.post_id,),
                ).fetchone()
                if post is None:
                    raise ValueError("Post no longer available")
                history = list_observations(db, self.post_id)
            self.post_label.setText(
                f"Exact Post: {self.post_id} • {post[0]} • {post[1]} • published_at={post[2] or 'NULL'}"
            )
            self._eligible = post[1] in ("PUBLISHED", "REMOVED") and post[2] is not None
            self._history = {o.observation_id: o for o in history}
            self.history.blockSignals(True)
            self.history.clear()
            self.history.addItem("Lần đo mới — chưa lưu", None)
            for o in history:
                self.history.addItem(
                    f"{o.observed_at} • {o.collector_type} • {o.observation_id}",
                    o.observation_id,
                )
            self.history.setCurrentIndex(max(0, self.history.findData(selected)))
            self.history.blockSignals(False)
            self._loaded = True
            self._select()
        except Exception as exc:
            self._loaded = False
            self.status.setText(f"Chưa đọc được lịch sử: {exc}")
            self._refresh()
        return self._loaded

    def _select(self, *_args: object) -> None:
        if not self._loaded or self._worker is not None:
            return
        event = self._history.get(str(self.history.currentData()))
        self.metrics.setRowCount(0)
        self.observed_at.setText(event.observed_at if event else _timestamp())
        if event:
            self.provenance.setText(
                f"{event.observation_id} • collector={event.collector_type} • "
                f"run={event.processing_run_id or 'NULL'} • created_at={event.created_at}"
            )
            for m in event.metrics:
                self._row(
                    (
                        m.metric_key,
                        m.metric_scope,
                        str(m.numeric_value),
                        m.unit,
                        m.definition_version,
                        m.raw_value or "",
                    )
                )
            self.status.setText(
                "Snapshot chỉ đọc. Metric không có hàng = NULL/chưa biết, không phải 0."
            )
        else:
            self.provenance.setText("Lần đo mới: MANUAL • processing_run_id=NULL")
            self.status.setText(
                "Nhập số đã quan sát cùng unit/definition; không sửa lần đo cũ."
            )
        self._refresh()

    def _row(self, values: tuple[str, ...]) -> None:
        row = self.metrics.rowCount()
        self.metrics.insertRow(row)
        for col, value in enumerate(values):
            self.metrics.setItem(row, col, QTableWidgetItem(value))

    def _refresh(self) -> None:
        idle = self._worker is None
        new = self.history.currentData() is None
        editing = idle and self._loaded and self._eligible and new
        self.history.setEnabled(idle and self._loaded)
        self.new_button.setEnabled(idle and self._loaded)
        self.reload_button.setEnabled(idle)
        self.buttons.setEnabled(idle)
        self.observed_at.setReadOnly(not editing)
        self.metrics.setEditTriggers(
            QTableWidget.EditTrigger.AllEditTriggers
            if editing
            else QTableWidget.EditTrigger.NoEditTriggers
        )
        for button in (self.add_button, self.remove_button, self.save_button):
            button.setEnabled(editing)

    def new_observation(self) -> None:
        if self._loaded and self._worker is None:
            self.history.setCurrentIndex(0)
            self._select()

    def add_metric(self) -> None:
        if self.add_button.isEnabled():
            self._row(("", "POST", "", "count", "", ""))

    def remove_metric(self) -> None:
        if self.remove_button.isEnabled() and self.metrics.currentRow() >= 0:
            self.metrics.removeRow(self.metrics.currentRow())

    def save(self) -> None:
        if not self.save_button.isEnabled():
            return
        try:
            payload: list[MetricInput] = []
            for row in range(self.metrics.rowCount()):
                values = [
                    self.metrics.item(row, col).text()
                    if self.metrics.item(row, col)
                    else ""
                    for col in range(6)
                ]
                key, scope, number, unit, definition, raw = values
                # Parse whole numbers as int first, so precision loss cannot be hidden by float().
                numeric: float | None = None
                if number.strip():
                    try:
                        numeric = int(number)
                    except ValueError:
                        numeric = float(number)
                payload.append(
                    MetricInput(
                        key,
                        scope,
                        numeric,
                        unit,
                        definition,
                        raw or (number if number.strip() else None),
                    )
                )
        except ValueError:
            self.status.setText(
                "Số metric không hợp lệ; dùng số hoặc để trống khi chưa biết."
            )
            return
        self._result, self._failure = None, None
        self._worker = ObservationWorker(
            self.root, self.post_id, self.observed_at.text(), tuple(payload)
        )
        self._worker.completed.connect(self._completed)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._finish)
        self.status.setText("Đang lưu lần đo mới…")
        self._refresh()
        self._worker.start()

    def _completed(self, result: Observation) -> None:
        self._result = result

    def _failed(self, message: str) -> None:
        self._failure = message

    def _finish(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.deleteLater()
        if self._failure is not None:
            self.status.setText(f"Không lưu được: {self._failure}")
            self._refresh()
            QMessageBox.warning(self, "Observation", self.status.text())
        elif self._result is not None:
            identity = self._result.observation_id
            if self.reload(identity):
                self.status.setText(f"Đã lưu lần đo {identity}; lịch sử cũ giữ nguyên.")
            else:
                self._loaded = False
                self.status.setText(
                    f"Đã lưu {identity}, chưa đọc lại được; cần Đọc lại trước khi tiếp tục."
                )
                self._refresh()
        else:
            self._loaded = False
            self.status.setText("Worker chưa trả kết quả; cần Đọc lại.")
            self._refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self) -> None:
        if self._worker is None:
            super().reject()

    def done(self, result: int) -> None:
        if self._worker is None:
            super().done(result)
