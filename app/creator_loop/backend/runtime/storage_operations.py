"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

from creator_loop.backend.library.storage_relocation import relocate_asset_files
from creator_loop.backend.library.storage_roots import (
    StorageRootError,
    default_storage_root_id,
    list_storage_roots,
    register_storage_root,
    resolve_registered_root,
    set_default_storage_root,
)
from creator_loop.backend.runtime.database import open_readonly


def run_StorageWorker(self: Any) -> None:
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
            message = f"Đã chuyển vị trí {len(moved)} file; giữ nguyên ID và nguồn cũ."
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
