"""Choose and inspect a normalized region on a verified image snapshot."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from creator_loop.image_evidence import crop_image_region


def _scaled(image: QImage, width: int, height: int) -> QPixmap:
    return QPixmap.fromImage(image).scaled(
        width,
        height,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class ImageEvidenceDialog(QDialog):
    def __init__(
        self,
        image: QImage,
        *,
        correction: bool = False,
        region: dict[str, float] | None = None,
        content: str = "",
    ) -> None:
        super().__init__()
        self.image = image
        self.correction = correction
        self._original_region = dict(region) if region is not None else None
        self._edited_coordinates: set[str] = set()
        self.setWindowTitle(
            "Sửa Evidence Image" if correction else "Tạo Evidence từ Image"
        )
        self.resize(720, 660)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Vùng ảnh đã chuẩn hóa orientation; tọa độ từ 0 đến 1.")
        )
        original = QLabel()
        original.setAlignment(Qt.AlignmentFlag.AlignCenter)
        original.setPixmap(_scaled(image, 640, 240))
        layout.addWidget(original)
        form = QFormLayout()
        self.coords: dict[str, QDoubleSpinBox] = {}
        for key, label, default in (
            ("x", "X", 0.0),
            ("y", "Y", 0.0),
            ("width", "Rộng", 1.0),
            ("height", "Cao", 1.0),
        ):
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1.0)
            spin.setDecimals(16)
            spin.setMinimumWidth(180)
            spin.setSingleStep(0.01)
            spin.setValue(default if region is None else region[key])
            spin.valueChanged.connect(
                lambda _value, coordinate=key: self._coordinate_changed(coordinate)
            )
            self.coords[key] = spin
            form.addRow(label, spin)
        self.content = QLineEdit(content)
        self.actor = QLineEdit("creator")
        self.reason = QLineEdit()
        form.addRow("Quan sát", self.content)
        form.addRow("Người sửa" if correction else "Người tạo", self.actor)
        if correction:
            form.addRow("Lý do sửa", self.reason)
        layout.addLayout(form)
        layout.addWidget(QLabel("Vùng sẽ lưu:"))
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(170)
        layout.addWidget(self.preview)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.content.textChanged.connect(self._refresh)
        self.actor.textChanged.connect(self._refresh)
        if correction:
            self.reason.textChanged.connect(self._refresh)
        self._refresh()

    def region(self) -> dict[str, float]:
        return {
            key: (
                self._original_region[key]
                if self._original_region is not None
                and key not in self._edited_coordinates
                else spin.value()
            )
            for key, spin in self.coords.items()
        }

    def _coordinate_changed(self, coordinate: str) -> None:
        self._edited_coordinates.add(coordinate)
        self._refresh()

    def _refresh(self) -> None:
        region = self.region()
        x, y, width, height = (region[k] for k in ("x", "y", "width", "height"))
        valid_region = (
            x >= 0
            and y >= 0
            and width > 0
            and height > 0
            and x + width <= 1
            and y + height <= 1
        )
        crop = None
        if valid_region:
            try:
                crop = crop_image_region(self.image, region)
            except ValueError:
                valid_region = False
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            valid_region
            and bool(self.content.text().strip())
            and bool(self.actor.text().strip())
            and (not self.correction or bool(self.reason.text().strip()))
        )
        if not valid_region or crop is None:
            self.preview.setText("Vùng phải nằm trong ảnh.")
            return
        self.preview.setPixmap(_scaled(crop, 640, 165))


class ImageRegionView(QDialog):
    def __init__(self, content: str, crop: QImage) -> None:
        super().__init__()
        self.setWindowTitle("Vùng Evidence đã xác minh")
        self.resize(680, 480)
        layout = QVBoxLayout(self)
        description = QLabel(content)
        description.setWordWrap(True)
        layout.addWidget(description)
        self.region_view = QLabel()
        self.region_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.region_view.setPixmap(_scaled(crop, 640, 390))
        layout.addWidget(self.region_view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
