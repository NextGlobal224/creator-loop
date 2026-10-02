"""Show an Asset's Sources and collect explicit provenance from the user."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from creator_loop.database import open_readonly
from creator_loop.source_association import SourceDetails


class SourceDialog(QDialog):
    def __init__(self, root: Path, asset_id: str, asset_name: str) -> None:
        super().__init__()
        self.setWindowTitle("Nguồn của Asset")
        self.resize(720, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Asset: {asset_name}"))
        self.links = QTableWidget(0, 4)
        self.links.setHorizontalHeaderLabels(
            ("Nền tảng", "URL / ID", "Quyền", "Quan hệ")
        )
        self.links.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.links.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.links)

        with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
            linked = db.execute(
                """SELECT s.platform, COALESCE(s.canonical_url,s.external_id,''),
                          s.rights_status,sa.relationship_type
                   FROM source_assets sa JOIN sources s ON s.source_id=sa.source_id
                   WHERE sa.asset_id=? ORDER BY sa.recorded_at,s.source_id""",
                (asset_id,),
            ).fetchall()
            all_sources = db.execute(
                """SELECT source_id,platform,COALESCE(canonical_url,external_id,'')
                   FROM sources ORDER BY created_at,source_id"""
            ).fetchall()
        self.links.setRowCount(len(linked))
        for row_index, row in enumerate(linked):
            for column_index, value in enumerate(row):
                self.links.setItem(
                    row_index, column_index, QTableWidgetItem(str(value))
                )

        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("Tạo Source mới", "new")
        if all_sources:
            self.mode.addItem("Nối Source đã có", "existing")
        form.addRow("Cách gắn", self.mode)
        self.existing = QComboBox()
        for source_id, platform, reference in all_sources:
            self.existing.addItem(f"{platform} — {reference or source_id}", source_id)
        form.addRow("Source đã có", self.existing)
        self.platform = QLineEdit()
        form.addRow("Nền tảng / xuất xứ", self.platform)
        self.url = QLineEdit()
        form.addRow("URL (nếu biết)", self.url)
        self.external_id = QLineEdit()
        form.addRow("ID bên ngoài (nếu biết)", self.external_id)
        self.publisher = QLineEdit()
        form.addRow("Tác giả / nơi đăng (nếu biết)", self.publisher)
        self.rights = QComboBox()
        for status in (
            "UNKNOWN",
            "OWNED",
            "LICENSED",
            "REFERENCE_ONLY",
            "RESTRICTED",
        ):
            self.rights.addItem(status)
        form.addRow("Quyền", self.rights)
        self.relationship = QComboBox()
        for relationship in ("UNKNOWN", "ORIGIN", "REPOST", "REFERENCE"):
            self.relationship.addItem(relationship)
        form.addRow("Quan hệ với Asset", self.relationship)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.mode.currentIndexChanged.connect(self._refresh)
        self.platform.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        creating = self.mode.currentData() == "new"
        for widget in (
            self.platform,
            self.url,
            self.external_id,
            self.publisher,
            self.rights,
        ):
            widget.setEnabled(creating)
        self.existing.setEnabled(not creating)
        valid = (
            bool(self.platform.text().strip())
            if creating
            else self.existing.count() > 0
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)

    def source_id(self) -> str | None:
        return (
            str(self.existing.currentData())
            if self.mode.currentData() == "existing"
            else None
        )

    def details(self) -> SourceDetails:
        return SourceDetails(
            platform=self.platform.text(),
            canonical_url=self.url.text(),
            external_id=self.external_id.text(),
            publisher_name=self.publisher.text(),
            rights_status=self.rights.currentText(),
        )
