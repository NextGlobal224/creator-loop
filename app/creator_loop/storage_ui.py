"""User-controlled registered media stores and verified relocation."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from creator_loop.database import open_readonly
from creator_loop.storage_relocation import relocate_asset_files
from creator_loop.storage_roots import (
    StorageRootError,
    default_storage_root_id,
    list_storage_roots,
    register_storage_root,
    resolve_registered_root,
    set_default_storage_root,
)


class StorageWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(
        self, root: Path, action: str, value: str = "", asset_id: str = ""
    ) -> None:
        super().__init__()
        self.root, self.action, self.value, self.asset_id = (
            root,
            action,
            value,
            asset_id,
        )

    def run(self) -> None:
        try:
            message = "Đã kiểm tra các kho media."
            if self.action == "register":
                register_storage_root(self.root, Path(self.value))
                message = "Đã đăng ký thư mục; nội dung có sẵn được giữ nguyên."
            elif self.action == "relocate":
                with closing(open_readonly(self.root / "creator_loop.sqlite3")) as db:
                    files = [
                        row[0]
                        for row in db.execute(
                            "SELECT file_id FROM asset_files WHERE asset_id=? ORDER BY file_id",
                            (self.asset_id,),
                        )
                    ]
                moved = relocate_asset_files(
                    self.root, files, self.value, cancelled=self.isInterruptionRequested
                )
                message = (
                    f"Đã chuyển vị trí {len(moved)} file; giữ nguyên ID và nguồn cũ."
                )
            elif self.action == "default":
                set_default_storage_root(self.root, self.value or None)
                message = "Đã chọn kho cho media nhập mới và file dẫn xuất; file cũ giữ nguyên vị trí."
            elif self.action != "list":
                raise ValueError("Unknown storage action")
            rows = []
            for registered in list_storage_roots(self.root):
                try:
                    resolve_registered_root(self.root, registered.root_id)
                    available = True
                except (StorageRootError, OSError):
                    available = False
                rows.append((registered.root_id, registered.path, available))
            self.result.emit((message, rows, default_storage_root_id(self.root)))
        except InterruptedError:
            self.failed.emit(
                "Đã hủy chuyển media; vị trí đã lưu và nguồn cũ được giữ nguyên."
            )
        except Exception as exc:
            self.failed.emit(f"Không thể hoàn tất thao tác kho media: {exc}")


class StorageDialog(QDialog):
    def __init__(self, root: Path, asset_id: str = "") -> None:
        super().__init__()
        self.root, self.asset_id = root, asset_id
        self._worker: StorageWorker | None = None
        self._result: object | None = None
        self._failure = ""
        self.setWindowTitle("Kho media — vị trí lưu")
        self.resize(720, 300)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Đăng ký thư mục media có sẵn ngoài thư mục dữ liệu ứng dụng.")
        )
        self.register_button = QPushButton("Chọn thư mục để đăng ký")
        self.register_button.clicked.connect(self.choose_folder)
        layout.addWidget(self.register_button)
        self.stores = QComboBox()
        self.stores.currentIndexChanged.connect(self._controls)
        layout.addWidget(self.stores)
        self.refresh_button = QPushButton("Kiểm tra lại kết nối kho")
        self.refresh_button.clicked.connect(lambda: self.start("list"))
        layout.addWidget(self.refresh_button)
        self.default_button = QPushButton("Dùng kho đã chọn cho media mới")
        self.default_button.clicked.connect(self.choose_default)
        layout.addWidget(self.default_button)
        self.local_button = QPushButton("Dùng thư mục dữ liệu ứng dụng cho media mới")
        self.local_button.clicked.connect(lambda: self.start("default"))
        layout.addWidget(self.local_button)
        self.default_status = QLabel()
        self.default_status.setWordWrap(True)
        layout.addWidget(self.default_status)
        self.confirm = QCheckBox(
            "Chuyển các file hiện có của Asset đã chọn; giữ bản nguồn cũ."
        )
        self.confirm.toggled.connect(self._controls)
        layout.addWidget(self.confirm)
        self.move_button = QPushButton("Chuyển media của Asset đã chọn")
        self.move_button.clicked.connect(self.move_selected)
        layout.addWidget(self.move_button)
        self.cancel_button = QPushButton("Hủy chuyển media")
        self.cancel_button.clicked.connect(self.cancel_move)
        layout.addWidget(self.cancel_button)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.start("list")

    def _controls(self) -> None:
        busy = self._worker is not None
        selection = self.stores.currentData()
        self.register_button.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.stores.setEnabled(not busy)
        self.confirm.setEnabled(not busy and bool(self.asset_id))
        self.move_button.setEnabled(
            not busy
            and bool(self.asset_id)
            and bool(selection)
            and selection[1]
            and self.confirm.isChecked()
        )
        self.buttons.setEnabled(not busy)
        self.default_button.setEnabled(not busy and bool(selection) and selection[1])
        self.local_button.setEnabled(not busy)
        self.cancel_button.setEnabled(
            busy and self._worker is not None and self._worker.action == "relocate"
        )

    def cancel_move(self) -> None:
        if self._worker is not None and self._worker.action == "relocate":
            self._worker.requestInterruption()
            self.status.setText("Đang hủy; chờ rollback và đóng file an toàn…")

    def choose_default(self) -> None:
        selection = self.stores.currentData()
        if self._worker is None and selection and selection[1]:
            self.start("default", selection[0])

    def choose_folder(self) -> None:
        if self._worker is not None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Chọn kho media")
        if folder:
            self.start("register", folder)

    def move_selected(self) -> None:
        selected = self.stores.currentData()
        if (
            self._worker is not None
            or not self.asset_id
            or not selected
            or not selected[1]
            or not self.confirm.isChecked()
        ):
            return
        self.start("relocate", selected[0])

    def start(self, action: str, value: str = "") -> None:
        if self._worker is not None:
            return
        self._result, self._failure = None, ""
        worker = StorageWorker(self.root, action, value, self.asset_id)
        self._worker = worker
        worker.result.connect(self._store_result)
        worker.failed.connect(self._store_failure)
        worker.finished.connect(self._finished)
        self.status.setText("Đang kiểm tra/copy và xác minh media…")
        self._controls()
        worker.start()

    def _store_result(self, value: object) -> None:
        self._result = value

    def _store_failure(self, message: str) -> None:
        self._failure = message

    def _finished(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        if isinstance(self._result, tuple):
            message, rows, default = self._result
            selected = self.stores.currentData()
            self.stores.clear()
            for root_id, path, available in rows:
                label = f"{path} — {'Sẵn sàng' if available else 'Unavailable: mất/đổi volume'}"
                self.stores.addItem(label, (root_id, available))
                if selected and selected[0] == root_id:
                    self.stores.setCurrentIndex(self.stores.count() - 1)
            self.status.setText(message)
            chosen = next(
                (path for root_id, path, _available in rows if root_id == default), None
            )
            self.default_status.setText(
                f"Media mới: {chosen or 'thư mục dữ liệu ứng dụng'}. Kho mất kết nối sẽ báo lỗi, không tự đổi kho."
            )
        else:
            self.status.setText(self._failure or "Chưa xác minh kết quả thao tác.")
        self.confirm.setChecked(False)
        self._controls()

    def reject(self) -> None:
        if self._worker is None:
            super().reject()

    def done(self, result: int) -> None:
        if self._worker is not None:
            self.status.setText("Chờ thao tác kho media kết thúc trước khi đóng.")
            return
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)
