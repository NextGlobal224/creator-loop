"""Responsive Source collection, isolated browser ownership and bounded stop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from creator_loop.backend.facebook.facebook_operations import (
    read_terminal as read_terminal,
)
from creator_loop.backend.facebook.facebook_operations import (
    run_FacebookWorker,
)
from creator_loop.backend.facebook.facebook_research import (
    DEFAULT_SCAN_BUDGETS,
    DEFAULT_THRESHOLDS,
    MAX_SCAN_BUDGETS,
    METRICS,
    candidates,
    criteria,
    export_viral_results,
    hashtag_summary,
    reassessed_candidates,
    research_counts,
    save_selected,
    saved_assessment,
    searches,
    suggested_queries,
)
from creator_loop.backend.facebook.facebook_sources import (
    post_link,
    saved_sources,
)
from creator_loop.frontend.owned_thread import OwnedThreadExit


class FacebookWorker(QThread):
    progress = Signal(str)
    completed = Signal(str)

    def __init__(
        self,
        root: Path,
        mode: str,
        keyword: str,
        limit: int,
        url: str | None = None,
        transport: str = "private",
        target_id: str | None = None,
        research: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.root = root
        self.mode = mode
        self.keyword = keyword
        self.limit = limit
        self.url = url
        self.transport = transport
        self.target_id = target_id
        self.research = research
        self.connection_result: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.session_ready = False

    def run(self) -> None:
        run_FacebookWorker(self)


class FacebookSelectionWorker(FacebookWorker):
    def __init__(self, root: Path, ids: list[str]) -> None:
        super().__init__(root, "save", "", len(ids), transport="existing")
        self.ids = ids

    def run(self) -> None:
        saved = duplicates = 0
        try:
            for cid in self.ids:
                _, new = save_selected(self.root, cid, self.isInterruptionRequested)
                saved += int(new)
                duplicates += int(not new)
                self.progress.emit(f"Đã lưu {saved}; bỏ qua {duplicates} nguồn trùng.")
            self.completed.emit(
                f"Lưu đã chọn: {saved} mới, {duplicates} nguồn trùng. Đánh giá và first capture được giữ riêng."
            )
        except InterruptedError:
            self.completed.emit(
                f"Đã hủy lưu; giữ {saved} nguồn đã commit, Chrome vẫn mở."
            )
        except Exception:
            self.completed.emit(
                f"Không hoàn tất lưu; giữ {saved} nguồn đã commit. Không tự duyệt Evidence."
            )


class FacebookDialog(QDialog):
    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.worker: FacebookWorker | None = None
        self.guard: OwnedThreadExit | None = None
        self.close_after = False
        self.verified_target: str | None = None
        self.current_search: str | None = None
        self.candidate_rows: list[dict[str, Any]] = []
        self._candidate_choices: dict[str, bool] = {}
        self._candidate_url: str | None = None
        self.setWindowTitle("Tư liệu Facebook — trình duyệt local riêng")
        self.resize(1250, 900)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Lưu link và trích đoạn nhìn thấy; không tải video/ảnh, không tự tạo Evidence ACCEPT."
            )
        )
        connection = QHBoxLayout()
        self.browser_mode = QComboBox()
        self.browser_mode.setObjectName("facebookBrowserMode")
        self.browser_mode.addItem("Chrome đang mở", "existing")
        self.browser_mode.addItem("Trình duyệt riêng", "private")
        self.chrome_tabs = QComboBox()
        self.chrome_tabs.setObjectName("facebookChromeTab")
        self.chrome_tabs.addItem("Kết nối để chọn tab Facebook", None)
        self.chrome_tabs.currentIndexChanged.connect(
            lambda: setattr(self, "verified_target", None)
        )
        connection.addWidget(self.browser_mode)
        connection.addWidget(self.chrome_tabs, 1)
        layout.addLayout(connection)
        controls = QHBoxLayout()
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("Từ khóa tìm bài Facebook")
        self.keyword.setMaxLength(200)
        self.keyword.setObjectName("facebookKeyword")
        self.limit = QSpinBox()
        self.limit.setRange(1, MAX_SCAN_BUDGETS["max_details"])
        self.limit.setValue(DEFAULT_SCAN_BUDGETS["max_details"])
        self.limit.setObjectName("facebookLimit")
        self.limit.setToolTip(
            "Tổng số lần mở chi tiết, bao gồm timestamp và lượt mở rộng hashtag"
        )
        self.keyword.setPlaceholderText("Chủ đề tìm tư liệu (không cố định địa danh)")
        controls.addWidget(self.keyword)
        controls.addWidget(self.limit)
        self.scan = QPushButton("Quét")
        self.scan.setObjectName("facebookScan")
        self.scan.clicked.connect(lambda: self.start("scan"))
        self.login = QPushButton("Kết nối Chrome đang mở")
        self.login.setObjectName("facebookConnect")
        self.login.clicked.connect(
            lambda: self.start(
                "connect" if self.browser_mode.currentData() == "existing" else "login"
            )
        )
        self.ready = QPushButton("Đã đăng nhập")
        self.ready.setEnabled(False)
        self.ready.clicked.connect(self.login_ready)
        self.stop = QPushButton("Dừng")
        self.stop.setObjectName("facebookStop")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        for b in (self.scan, self.login, self.ready, self.stop):
            controls.addWidget(b)
        layout.addLayout(controls)
        research_row = QHBoxLayout()
        self.purpose = QLineEdit()
        self.purpose.setObjectName("facebookPurpose")
        self.purpose.setMaxLength(500)
        self.purpose.setPlaceholderText("Mục đích sử dụng / điều muốn học từ tư liệu")
        self.since = QLineEdit()
        self.since.setObjectName("facebookSince")
        self.since.setPlaceholderText("Đăng từ YYYY-MM-DD")
        self.since.setMaxLength(10)
        self.until = QLineEdit()
        self.until.setObjectName("facebookUntil")
        self.until.setPlaceholderText("Đến YYYY-MM-DD")
        self.until.setMaxLength(10)
        self.research_mode = QComboBox()
        self.research_mode.setObjectName("facebookResearchMode")
        for label, value in (
            ("Kết hợp", "COMBINED"),
            ("Dấu hiệu viral", "VIRAL"),
            ("Giá trị phân tích", "ANALYSIS"),
        ):
            self.research_mode.addItem(label, value)
        self.research_mode.setCurrentIndex(self.research_mode.findData("VIRAL"))
        research_row.addWidget(self.purpose, 2)
        research_row.addWidget(self.since)
        research_row.addWidget(self.until)
        research_row.addWidget(self.research_mode)
        layout.addLayout(research_row)
        budget = QHBoxLayout()
        self.query_limit = QSpinBox()
        self.query_limit.setObjectName("facebookQueryLimit")
        self.query_limit.setRange(1, MAX_SCAN_BUDGETS["max_queries"])
        self.query_limit.setValue(DEFAULT_SCAN_BUDGETS["max_queries"])
        self.candidate_limit = QSpinBox()
        self.candidate_limit.setObjectName("facebookCandidateLimit")
        self.candidate_limit.setRange(1, MAX_SCAN_BUDGETS["max_candidates"])
        self.candidate_limit.setValue(DEFAULT_SCAN_BUDGETS["max_candidates"])
        budget.addWidget(QLabel("Tối đa truy vấn"))
        budget.addWidget(self.query_limit)
        budget.addWidget(QLabel("Ứng viên"))
        budget.addWidget(self.candidate_limit)
        budget.addWidget(QLabel("Mở chi tiết: ô bên trên"))
        self.thresholds: dict[str, QSpinBox] = {}
        for name, label in zip(
            METRICS, ("Thích", "Bình luận", "Chia sẻ", "Lượt xem"), strict=True
        ):
            spin = QSpinBox()
            spin.setObjectName("facebookThreshold" + name.title())
            spin.setRange(1, 1000000000)
            spin.setValue(DEFAULT_THRESHOLDS[name])
            self.thresholds[name] = spin
            budget.addWidget(QLabel(label + " ≥"))
            budget.addWidget(spin)
        layout.addLayout(budget)
        self.query_editor = QPlainTextEdit()
        self.query_editor.setObjectName("facebookQueries")
        self.query_editor.setPlaceholderText(
            "Truy vấn gợi ý có thể sửa, mỗi dòng một truy vấn; đây không phải hashtag có nguồn. Một chỗ được dành cho hashtag quan sát được nếu còn budget."
        )
        self.query_editor.setMaximumHeight(65)
        self.suggest = QPushButton("Gợi ý truy vấn")
        self.suggest.setObjectName("facebookSuggest")
        self.suggest.clicked.connect(self.suggest_search)
        query_row = QHBoxLayout()
        query_row.addWidget(self.query_editor, 1)
        query_row.addWidget(self.suggest)
        layout.addLayout(query_row)
        self.research_rule = QLabel()
        self.research_rule.setObjectName("facebookResearchRule")
        self.research_rule.setWordWrap(True)
        layout.addWidget(self.research_rule)
        self.research_mode.currentIndexChanged.connect(self.update_rule)
        self.update_rule()
        self.counts_label = QLabel()
        self.counts_label.setObjectName("facebookResearchCounts")
        self.counts_label.setWordWrap(True)
        layout.addWidget(self.counts_label)
        self.status = QLabel(
            "Sẵn sàng. Kết nối tab Facebook trong Chrome đang mở trước khi Quét."
        )
        self.status.setWordWrap(True)
        self.status.setObjectName("facebookStatus")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("facebookSources")
        self.table.setHorizontalHeaderLabels(
            ("URL bài thật", "Nguồn", "Trích đoạn", "Từ khóa", "Thu thập UTC")
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self.show_saved_assessment)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.tabs = QTabWidget()
        self.candidate_table = QTableWidget(0, 6)
        self.candidate_table.setObjectName("facebookCandidates")
        self.candidate_table.itemChanged.connect(lambda _: self.update_counts())
        self.candidate_table.setHorizontalHeaderLabels(
            (
                "Chọn",
                "Quy tắc",
                "Nguồn / URL",
                "Thông tin đọc được",
                "Lý do / Chưa rõ",
                "Thu thập UTC",
            )
        )
        self.candidate_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.candidate_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.candidate_table.itemSelectionChanged.connect(self.show_candidate)
        self.tabs.addTab(self.candidate_table, "Ứng viên — chưa nhập Source")
        self.tabs.addTab(self.table, "Tư liệu đã lưu")
        self.hashtag_table = QTableWidget(0, 6)
        self.hashtag_table.setObjectName("facebookHashtags")
        self.hashtag_table.setHorizontalHeaderLabels(
            (
                "Hashtag có nguồn",
                "Bài sau chống trùng",
                "Nguồn độc lập",
                "Khoảng quan sát",
                "Nhãn trong mẫu",
                "Link nguồn",
            )
        )
        self.hashtag_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.hashtag_table.cellDoubleClicked.connect(self.open_hashtag_source)
        self.hashtag_table.setToolTip(
            "Bấm đúp để mở link nguồn đầu tiên của hashtag trong tab Facebook đã kết nối; mọi link được hiển thị ở cột Link nguồn."
        )
        self.tabs.addTab(
            self.hashtag_table, "Hashtag trong mẫu — không phải toàn Facebook"
        )
        layout.addWidget(self.tabs, 1)
        self.search_history = QComboBox()
        self.search_history.setObjectName("facebookSearchHistory")
        self.search_history.currentIndexChanged.connect(self.change_search)
        layout.addWidget(self.search_history)
        self.details = QTextEdit()
        self.details.setObjectName("facebookCandidateDetails")
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(155)
        layout.addWidget(self.details)
        actions = QHBoxLayout()
        self.save_candidates = QPushButton("Lưu đã chọn")
        self.save_candidates.setObjectName("facebookSaveSelected")
        self.save_candidates.clicked.connect(self.save_choices)
        self.open_candidate = QPushButton("Mở ứng viên")
        self.open_candidate.setObjectName("facebookOpenCandidate")
        self.open_candidate.clicked.connect(self.open_candidate_source)
        self.export_results = QPushButton("Xuất bảng bài đạt viral")
        self.export_results.setObjectName("facebookExportViral")
        self.export_results.clicked.connect(self.export_viral)
        actions.addWidget(self.save_candidates)
        actions.addWidget(self.open_candidate)
        actions.addWidget(self.export_results)
        layout.addLayout(actions)
        self.open_source = QPushButton("Mở nguồn")
        self.open_source.setObjectName("facebookOpen")
        self.open_source.clicked.connect(lambda: self.start("open"))
        layout.addWidget(self.open_source)
        close = QPushButton("Đóng")
        close.clicked.connect(self.close)
        layout.addWidget(close)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.reap)
        self.browser_mode.currentIndexChanged.connect(self.mode_changed)
        self.reload()

    def mode_changed(self) -> None:
        self.verified_target = None
        self.chrome_tabs.clear()
        self.chrome_tabs.addItem("Kết nối để chọn tab Facebook", None)
        existing = self.browser_mode.currentData() == "existing"
        self.chrome_tabs.setEnabled(existing)
        self.login.setText("Kết nối Chrome đang mở" if existing else "Đăng nhập riêng")
        self.status.setText(
            "Kết nối tab Facebook trong Chrome đang mở trước khi Quét."
            if existing
            else "Trình duyệt riêng dùng phiên trong data root."
        )

    def reload(self) -> None:
        self.rows = saved_sources(self.root)
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            for j, key in enumerate(
                ("canonical_url", "publisher_name", "excerpt", "keyword", "captured_at")
            ):
                item = QTableWidgetItem(str(row[key] or "Chưa rõ"))
                item.setToolTip(str(row[key] or ""))
                self.table.setItem(i, j, item)
        self.table.setColumnWidth(0, 310)
        self.table.setColumnWidth(2, 300)
        observations = candidates(self.root)
        previous = self.current_search
        self.search_history.blockSignals(True)
        self.search_history.clear()
        for search in searches(self.root):
            self.search_history.addItem(
                search["created_at"] + " — " + search["criteria"]["topic"],
                search["search_id"],
            )
        index = self.search_history.findData(previous) if previous else 0
        self.search_history.setCurrentIndex(max(0, index))
        self.search_history.blockSignals(False)
        self.current_search = self.search_history.currentData()
        self.reload_candidates(observations)

    def suggest_search(self) -> None:
        self.query_editor.setPlainText(
            "\n".join(
                suggested_queries(self.keyword.text(), self.purpose.text())[
                    : self.query_limit.value()
                ]
            )
        )

    def show_saved_assessment(self) -> None:
        index = self.table.currentRow()
        if 0 <= index < len(self.rows) and hasattr(self, "details"):
            value = saved_assessment(self.root, str(self.rows[index]["source_id"]))
            self.details.setPlainText(
                json.dumps(value, ensure_ascii=False, indent=2)
                if value
                else "Tư liệu cũ chưa có đánh giá theo tiêu chí mới. URL-only là tham chiếu; không suy ngược thành media hoặc Evidence ACCEPT."
            )

    def settings(self) -> dict[str, Any]:
        queries = [
            q.strip() for q in self.query_editor.toPlainText().splitlines() if q.strip()
        ]
        if not queries:
            self.suggest_search()
            queries = self.query_editor.toPlainText().splitlines()
        return criteria(
            {
                "topic": self.keyword.text(),
                "purpose": self.purpose.text(),
                "since": self.since.text().strip() or None,
                "until": self.until.text().strip() or None,
                "mode": self.research_mode.currentData(),
                "queries": queries,
                "max_queries": self.query_limit.value(),
                "max_candidates": self.candidate_limit.value(),
                "max_details": self.limit.value(),
                "thresholds": {
                    key: spin.value() for key, spin in self.thresholds.items()
                },
            }
        )

    def update_rule(self) -> None:
        mode = self.research_mode.currentData()
        condition = {
            "VIRAL": "ĐÚNG CHỦ ĐỀ VÀ ĐẠT VIRAL; phân tích không thay thế viral.",
            "ANALYSIS": "Đúng chủ đề VÀ có giá trị phân tích; không đồng nghĩa đạt viral.",
            "COMBINED": "Đúng chủ đề VÀ (viral HOẶC phân tích); số đạt viral tính riêng.",
        }[mode]
        self.research_rule.setText(
            condition
            + " Viral: ít nhất một số liệu của chính bài đạt ngưỡng bên trên. "
            "Thiếu dữ liệu: Chưa rõ; video chỉ đánh giá caption."
        )

    def update_counts(self) -> None:
        rows = self.candidate_rows
        counts = research_counts(rows)
        selected = sum(
            pick.checkState() == Qt.CheckState.Checked
            for i in range(len(rows))
            if (pick := self.candidate_table.item(i, 0)) is not None
        )
        self.counts_label.setText(
            f"Ứng viên {counts['candidates']} | Đủ dữ liệu đánh giá viral {counts['evaluated']} "
            f"| Đúng chủ đề/ngày {counts['topical']} "
            f"| Đạt viral {counts['viral']} / mục tiêu 40 (tối thiểu 20) "
            f"| Đang chọn lưu {selected} | Đã lưu {sum(bool(r['saved']) for r in rows)}"
        )

    def change_search(self) -> None:
        self.current_search = self.search_history.currentData()
        self.reload_candidates(candidates(self.root))

    def export_viral(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self, "Xuất bảng bài đạt viral", "viral-results.csv", "CSV (*.csv)"
        )
        if target:
            try:
                count = export_viral_results(Path(target), self.candidate_rows)
                self.status.setText(f"Đã xuất {count} bài đạt viral: {target}")
            except (OSError, ValueError) as exc:
                self.status.setText("Không xuất bảng: " + str(exc))

    def reload_candidates(self, observations: list[dict[str, Any]]) -> None:
        choices = self._candidate_choices
        for i, previous in enumerate(self.candidate_rows):
            pick = self.candidate_table.item(i, 0)
            if pick is not None:
                choices[previous["candidate_id"]] = (
                    pick.checkState() == Qt.CheckState.Checked
                )
        self._candidate_choices = choices
        self.candidate_rows = reassessed_candidates(
            [r for r in observations if r["search_id"] == self.current_search]
        )
        self.candidate_table.setRowCount(len(self.candidate_rows))
        states = {
            "QUALIFIED": "Đạt quy tắc",
            "NEEDS_REVIEW": "Cần xem thêm",
            "REJECTED": "Không đạt quy tắc",
        }
        for i, row in enumerate(self.candidate_rows):
            checked = not row["saved"] and choices.get(
                row["candidate_id"],
                row["criteria"]["mode"] != "VIRAL"
                and row["assessment"]["state"] == "QUALIFIED",
            )
            choices[row["candidate_id"]] = checked
            pick = QTableWidgetItem()
            pick.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
            )
            pick.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.candidate_table.setItem(i, 0, pick)
            metrics = "; ".join(
                name
                + ": "
                + (
                    value["raw"] + (" (xấp xỉ)" if value["approximate"] else "")
                    if value
                    else "Chưa rõ"
                )
                for name, value in row["metrics"].items()
            )
            label = states[row["assessment"]["state"]]
            if row["criteria"]["mode"] == "VIRAL":
                label = (
                    "Đạt viral"
                    if research_counts([row])["viral"]
                    else "Không đạt chủ đề/ngày"
                    if row["assessment"]["topical"] is False
                    or row["assessment"]["time_match"] is False
                    else "Không đạt ngưỡng viral"
                    if row["assessment"]["viral"] is False
                    else "Chưa đủ căn cứ viral"
                )
            values = (
                label + (" — đã lưu" if row["saved"] else ""),
                (row["publisher"] or "Nguồn: Chưa rõ") + "\n" + row["url"],
                (row["published_at"] or row["published_label"] or "Ngày đăng: Chưa rõ")
                + "\n"
                + metrics
                + "\n"
                + (row["excerpt"] or "Trích đoạn: Chưa rõ")[:220],
                "; ".join(row["assessment"]["reasons"])
                + "\nChưa rõ: "
                + ", ".join(row["assessment"]["missing"]),
                row["observed_at"],
            )
            for j, value in enumerate(values, 1):
                self.candidate_table.setItem(i, j, QTableWidgetItem(value))
        for col, width in enumerate((55, 145, 260, 320, 300, 160)):
            self.candidate_table.setColumnWidth(col, width)
        self.update_counts()
        summaries = hashtag_summary(self.candidate_rows)
        self.hashtag_table.setRowCount(len(summaries))
        for i, summary in enumerate(summaries):
            independent = str(summary["known_independent_sources"]) + (
                f" đã biết; {summary['unknown_sources']} Chưa rõ"
                if summary["unknown_sources"]
                else ""
            )
            tag_values = (
                summary["tag"],
                str(summary["posts"]),
                independent,
                summary["observed_from"] + " → " + summary["observed_to"],
                summary["label"] + "; " + summary["growth"],
                "\n".join(summary["source_urls"]),
            )
            for j, value in enumerate(tag_values):
                self.hashtag_table.setItem(i, j, QTableWidgetItem(value))

    def show_candidate(self) -> None:
        row = self.candidate_table.currentRow()
        if 0 <= row < len(self.candidate_rows):
            item = self.candidate_rows[row]
            scope = (
                "Video: chỉ caption, chưa đánh giá toàn video.\n"
                if item["scope"] == "CAPTION_ONLY"
                else "Phạm vi: " + item["scope"] + "\n"
            )
            self.details.setPlainText(
                scope + json.dumps(item, ensure_ascii=False, indent=2)
            )

    def open_candidate_source(self) -> None:
        row = self.candidate_table.currentRow()
        if not 0 <= row < len(self.candidate_rows):
            self.status.setText("Chọn một ứng viên để mở nguồn.")
            return
        self._candidate_url = self.candidate_rows[row]["url"]
        self.start("open")
        self._candidate_url = None

    def open_hashtag_source(self, row: int, column: int) -> None:
        cell = self.hashtag_table.item(row, 5)
        if cell is not None and cell.text():
            url = cell.text().splitlines()[0]
            if post_link(url) is not None:
                self._candidate_url = url
                self.start("open")
                self._candidate_url = None

    def save_choices(self) -> None:
        if self.worker is not None:
            return
        selected = [
            row["candidate_id"]
            for i, row in enumerate(self.candidate_rows)
            if (pick := self.candidate_table.item(i, 0)) is not None
            and pick.checkState() == Qt.CheckState.Checked
        ]
        if not selected:
            self.status.setText(
                "Chọn ứng viên trước khi lưu; bài thiếu căn cứ không được tự chọn."
            )
            return
        worker = FacebookSelectionWorker(self.root, selected)
        self.worker = worker
        self.guard = OwnedThreadExit()
        worker.started.connect(self.guard.capture, Qt.ConnectionType.DirectConnection)
        worker.progress.connect(self.status.setText)
        worker.completed.connect(self.status.setText)
        for button in (
            self.scan,
            self.login,
            self.open_source,
            self.open_candidate,
            self.save_candidates,
        ):
            button.setEnabled(False)
        self.stop.setEnabled(True)
        worker.start()
        self.timer.start()

    def start(self, mode: str) -> None:
        if self.worker is not None:
            return
        keyword = self.keyword.text().strip()
        url = None
        if mode == "scan" and not keyword:
            self.status.setText("Nhập từ khóa trước khi Quét.")
            return
        transport = str(self.browser_mode.currentData())
        target_id = self.chrome_tabs.currentData() if transport == "existing" else None
        if mode == "open":
            row = self.table.currentRow()
            if self._candidate_url is None and not 0 <= row < len(self.rows):
                self.status.setText("Chọn một nguồn đã lưu.")
                return
            url = self._candidate_url or str(self.rows[row]["canonical_url"])
        if (
            transport == "existing"
            and mode in ("scan", "open")
            and (target_id is None or target_id != self.verified_target)
        ):
            self.status.setText(
                "Kết nối đúng tab Facebook trước khi Quét hoặc Mở nguồn."
            )
            return
        research = None
        limit = self.limit.value()
        if mode == "scan":
            try:
                research = self.settings()
            except ValueError:
                self.status.setText(
                    "Kiểm tra chủ đề, ngày YYYY-MM-DD, thứ tự ngày và số truy vấn trong budget."
                )
                return
            mode = "discover"
            limit = research["max_candidates"]
        worker = FacebookWorker(
            self.root, mode, keyword, limit, url, transport, target_id, research
        )
        self.worker = worker
        self.guard = OwnedThreadExit()
        worker.started.connect(self.guard.capture, Qt.ConnectionType.DirectConnection)
        worker.progress.connect(self.status.setText)
        worker.completed.connect(self.status.setText)
        for b in (
            self.scan,
            self.login,
            self.open_source,
            self.open_candidate,
            self.save_candidates,
        ):
            b.setEnabled(False)
        self.stop.setEnabled(True)
        self.ready.setEnabled(mode == "login")
        self.browser_mode.setEnabled(False)
        self.chrome_tabs.setEnabled(False)
        worker.start()
        self.timer.start()

    def login_ready(self) -> None:
        if self.worker is not None:
            self.worker.session_ready = True

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.requestInterruption()
            self.status.setText(
                "Đang dừng worker và ngắt kết nối; Chrome của bạn vẫn mở…"
                if self.browser_mode.currentData() == "existing"
                else "Đang đóng trình duyệt do ứng dụng sở hữu…"
            )
            self.stop.setEnabled(False)

    def reap(self) -> None:
        if self.worker is None:
            return
        if (
            self.guard is None
            or self.guard.error
            or not self.guard.terminal()
            or not self.worker.wait(0)
        ):
            return
        self.guard.close()
        self.guard = None
        self.worker.wait(0)
        if self.worker.mode == "discover" and self.worker.connection_result.get(
            "search_id"
        ):
            self.current_search = self.worker.connection_result["search_id"]
        if self.worker.transport == "existing":
            body = self.worker.connection_result
            if body.get("status") == "SELECT_TAB":
                self.chrome_tabs.clear()
                for tab in body["tabs"]:
                    self.chrome_tabs.addItem(
                        tab["title"] or "Facebook", tab["target_id"]
                    )
            elif body.get("status") == "SESSION_READY" and body.get("target_id"):
                self.chrome_tabs.clear()
                self.chrome_tabs.addItem("Tab Facebook đã kết nối", body["target_id"])
                self.verified_target = str(body["target_id"])
            elif body.get("status") in (
                "AUTH_REQUIRED",
                "CAPTCHA",
                "FACEBOOK_TAB_REQUIRED",
                "CONNECTION_REQUIRED",
                "SESSION_UNCONFIRMED",
            ):
                self.verified_target = None
        self.worker.deleteLater()
        self.worker = None
        self.timer.stop()
        self.ready.setEnabled(False)
        self.stop.setEnabled(False)
        self.browser_mode.setEnabled(True)
        self.chrome_tabs.setEnabled(self.browser_mode.currentData() == "existing")
        for b in (
            self.scan,
            self.login,
            self.open_source,
            self.open_candidate,
            self.save_candidates,
        ):
            b.setEnabled(True)
        self.reload()
        if self.close_after:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker is not None:
            self.close_after = True
            self.cancel()
            event.ignore()
        else:
            event.accept()
