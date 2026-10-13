"""Backend operations dispatched by thin UI workers."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from creator_loop.backend.facebook.facebook_browser import (
    LOGIN_SECONDS,
    OPEN_SECONDS,
    SCAN_SECONDS,
)
from creator_loop.backend.facebook.facebook_cdp import TARGET_ID
from creator_loop.backend.facebook.facebook_research import (
    research_counts,
    store_search,
    validate_candidate,
)
from creator_loop.backend.facebook.facebook_sources import (
    post_link,
    save_capture,
)
from creator_loop.backend.runtime.owned_process import OwnedWindowsProcess
from creator_loop.backend.runtime.runtime_ownership import (
    bind_workspace_child,
    create_workspace_marker,
)
from creator_loop.backend.runtime.windows_paths import file_io_path, resolve_file_path


def read_terminal(
    path: Path,
    run_id: str,
    exit_code: int,
    limit: int,
    research: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        path.is_symlink()
        or path.stat().st_size > (8 if research is not None else 2) * 1024**2
    ):
        raise ValueError("Invalid browser result")
    body = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(body, dict)
        or type(body.get("format")) is not int
        or body.get("format") != 1
        or body.get("run_id") != run_id
    ):
        raise ValueError("Browser result identity mismatch")
    codes = {
        "SUCCEEDED": 0,
        "SESSION_READY": 0,
        "CLOSED": 0,
        "CANCELLED": 2,
        "AUTH_REQUIRED": 3,
        "CAPTCHA": 3,
        "TIMED_OUT": 4,
        "BROWSER_FAILED": 5,
        "CONNECTION_REQUIRED": 7,
        "FACEBOOK_TAB_REQUIRED": 8,
        "SELECT_TAB": 0,
        "OPENED_KEEP_BROWSER": 0,
        "SESSION_UNCONFIRMED": 0,
    }
    status = body.get("status")
    if status not in codes or exit_code != codes[status]:
        raise ValueError("Browser terminal status/exit mismatch")
    if status == "SESSION_READY" and "target_id" in body:
        target = body["target_id"]
        if not isinstance(target, str) or not TARGET_ID.fullmatch(target):
            raise ValueError("Invalid connected target")
    if status == "SELECT_TAB":
        tabs = body.get("tabs")
        if not isinstance(tabs, list) or not 2 <= len(tabs) <= 50:
            raise ValueError("Invalid Facebook tab list")
        ids = set()
        for tab in tabs:
            if (
                not isinstance(tab, dict)
                or set(tab) != {"target_id", "title"}
                or not isinstance(tab["target_id"], str)
                or not TARGET_ID.fullmatch(tab["target_id"])
                or tab["target_id"] in ids
                or not isinstance(tab["title"], str)
                or len(tab["title"]) > 200
            ):
                raise ValueError("Invalid Facebook tab")
            ids.add(tab["target_id"])
    if status == "SUCCEEDED":
        if "candidates" in body:
            if (
                research is None
                or not isinstance(body["candidates"], list)
                or len(body["candidates"]) > limit
                or body.get("count") != len(body["candidates"])
            ):
                raise ValueError("Invalid discovery result")
            if (
                type(body.get("details_used")) is not int
                or not 0 <= body["details_used"] <= research["max_details"]
                or not isinstance(body.get("queries"), list)
                or len(body["queries"]) > research["max_queries"]
            ):
                raise ValueError("Discovery budget exceeded")
            query_strings = set()
            for q in body["queries"]:
                if (
                    not isinstance(q, dict)
                    or set(q) != {"text", "kind", "source_urls"}
                    or not isinstance(q["text"], str)
                    or not 1 <= len(q["text"]) <= 200
                    or q["kind"] not in ("EDITABLE_SUGGESTION", "OBSERVED_HASHTAG")
                    or not isinstance(q["source_urls"], list)
                    or len(q["source_urls"]) > 100
                    or any(post_link(url) is None for url in q["source_urls"])
                ):
                    raise ValueError("Invalid query provenance")
                query_strings.add(q["text"])
            ids = set()
            for item in body["candidates"]:
                validate_candidate(item, research)
                if item["external_id"] in ids or item["query"] not in query_strings:
                    raise ValueError("Invalid duplicate/query association")
                ids.add(item["external_id"])
            return body
        links = body.get("links")
        if (
            not isinstance(links, list)
            or not 0 <= len(links) <= limit
            or body.get("count") != len(links)
        ):
            raise ValueError("Invalid bounded post result")
        ids = set()
        for item in links:
            if not isinstance(item, dict) or set(item) != {
                "url",
                "excerpt",
                "publisher",
            }:
                raise ValueError("Invalid post result")
            link = post_link(item["url"])
            if link is None or link.url != item["url"] or link.external_id in ids:
                raise ValueError("Invalid/duplicate permalink")
            ids.add(link.external_id)
            for key, budget in (("excerpt", 8192), ("publisher", 200)):
                if item[key] is not None and (
                    not isinstance(item[key], str) or not 1 <= len(item[key]) <= budget
                ):
                    raise ValueError("Invalid post text")
    return body


def run_FacebookWorker(self: Any) -> None:
    self.progress.emit(
        "Đang kết nối Chrome; chọn Allow nếu Chrome hỏi…"
        if self.transport == "existing"
        else "Đang mở trình duyệt riêng…"
    )
    saved = duplicates = 0
    try:
        root = resolve_file_path(self.root, strict=True)
        for name in ("runtime", "logs"):
            folder = root / name
            folder.mkdir(exist_ok=True)
            if folder.is_symlink() or folder.is_junction():
                raise OSError("Real runtime/log directories required")
        run_id = uuid4().hex
        workspace = root / "runtime" / ("facebook-" + run_id)
        workspace.mkdir()
        self.workspace = workspace
        component = "facebook-browser/1.63.0/" + (
            "existing-cdp" if self.transport == "existing" else "chromium-1243"
        )
        marker = create_workspace_marker(
            root, workspace, component, kind="FACEBOOK_BROWSER_WORKSPACE"
        )
        request = workspace / "request.json"
        request.write_text(
            json.dumps(
                {
                    "format": 1,
                    "run_id": run_id,
                    "mode": self.mode,
                    "keyword": self.keyword,
                    "limit": self.limit,
                    "url": self.url,
                    "transport": self.transport,
                    "target_id": self.target_id,
                    **(
                        {"criteria": self.research} if self.research is not None else {}
                    ),
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("PLAYWRIGHT_NODEJS_PATH", None)
        env.pop("PWDEBUG", None)
        env.pop("NODE_OPTIONS", None)
        env.pop("DEBUG", None)
        prefix = [] if getattr(sys, "frozen", False) else ["-m", "creator_loop"]
        deadline = (
            time.monotonic()
            + {
                "scan": SCAN_SECONDS,
                "discover": 600,
                "login": LOGIN_SECONDS,
                "connect": LOGIN_SECONDS,
                "open": OPEN_SECONDS,
            }[self.mode]
            + 35
        )
        last = ""
        body: dict[str, Any] = {}
        with OwnedWindowsProcess(
            Path(sys.executable),
            [*prefix, "--facebook-browser-task", str(request)],
            root / "logs" / ("facebook-" + run_id),
            component_version=component,
            environment=env,
            capture_output=False,
            memory_limit_bytes=2 * 1024**3,
            before_resume=lambda child: bind_workspace_child(workspace, marker, child),
        ) as owned:
            stopped_at: float | None = None
            while True:
                if self.isInterruptionRequested():
                    file_io_path(workspace / "stop").touch(exist_ok=True)
                    if stopped_at is None:
                        stopped_at = time.monotonic()
                    elif time.monotonic() - stopped_at > 3:
                        owned.request_stop(2)
                if self.session_ready:
                    file_io_path(workspace / "ready").touch(exist_ok=True)
                result = workspace / "result.json"
                try:
                    if (
                        result.is_file()
                        and result.stat().st_size
                        < (8 if self.research is not None else 2) * 1024**2
                    ):
                        raw = result.read_text(encoding="utf-8")
                        update = json.loads(raw)
                        if update.get("run_id") == run_id and update.get("format") == 1:
                            body = update
                            if raw != last:
                                last = raw
                                status = str(body.get("status", ""))
                                if status == "SCANNING":
                                    self.progress.emit(
                                        f"Ứng viên {body.get('count', 0)}/{self.limit}; đủ dữ liệu {body.get('evaluated', 0)}; đạt viral {body.get('viral', 0)}; đang quét…"
                                    )
                                elif status == "WAIT_LOGIN":
                                    self.progress.emit(
                                        "Đăng nhập trực tiếp trong trình duyệt riêng, rồi bấm Đã đăng nhập. Không gửi mật khẩu cho ứng dụng."
                                    )
                                elif status == "OPENED":
                                    self.progress.emit(
                                        "Nguồn đang mở trong trình duyệt riêng; đóng cửa sổ hoặc bấm Dừng."
                                    )
                except (OSError, ValueError):
                    pass
                outcome = owned.poll()
                if outcome is not None:
                    break
                if time.monotonic() >= deadline:
                    owned.stop(124)
                    raise TimeoutError()
                self.msleep(100)
        if self.isInterruptionRequested():
            status = "CANCELLED"
        else:
            body = read_terminal(
                workspace / "result.json",
                run_id,
                outcome.exit_code,
                self.limit,
                self.research,
            )
            status = str(body["status"])
        self.connection_result = (
            body if body.get("status") == status else {"status": status}
        )
        if status == "SUCCEEDED":
            if self.mode == "discover" and self.research is not None:
                self.connection_result["search_id"] = store_search(
                    root, self.research, body["queries"], body["candidates"]
                )
            for item in body.get("links", []):
                if self.isInterruptionRequested():
                    status = "CANCELLED"
                    break
                _, new = save_capture(
                    root,
                    keyword=self.keyword,
                    url=item["url"],
                    excerpt=item.get("excerpt"),
                    publisher=item.get("publisher"),
                    cancelled=self.isInterruptionRequested,
                )
                saved += int(new)
                duplicates += int(not new)
                self.progress.emit(f"Đã lưu {saved}; bỏ qua {duplicates} link trùng…")
        messages = {
            "SUCCEEDED": f"Hoàn tất: lưu {saved}, bỏ qua {duplicates} link trùng.",
            "CANCELLED": f"Đã dừng. Giữ {saved} tư liệu đã lưu; Chrome đang mở của bạn được giữ nguyên.",
            "AUTH_REQUIRED": "Phiên Facebook đang mở cần xác thực lại. Đã dừng; Chrome được giữ nguyên."
            if self.transport == "existing"
            else "Phiên hết hạn hoặc cần đăng nhập. Đã dừng; bấm Đăng nhập riêng.",
            "CAPTCHA": "Facebook yêu cầu xác minh/CAPTCHA. Đã dừng; không tự retry.",
            "TIMED_OUT": "Đã dừng vì hết thời gian quét.",
            "SESSION_READY": "Đã kết nối đúng tab Facebook trong Chrome đang mở. Có thể Quét."
            if self.transport == "existing"
            else "Đã lưu phiên riêng. Có thể Quét.",
            "CLOSED": "Trình duyệt đã đóng.",
            "OPENED": "Nguồn đã đóng.",
            "BROWSER_FAILED": "Không kết nối được; Chrome của bạn vẫn mở. Kiểm tra Remote Debugging và chọn Allow khi Chrome hỏi."
            if self.transport == "existing"
            else "Trình duyệt không hoàn tất. Đã đóng cây tiến trình riêng.",
            "SESSION_UNCONFIRMED": "Chưa xác nhận được phiên Facebook trên tab đã chọn. Chrome được giữ mở; chưa cho phép Quét.",
            "CONNECTION_REQUIRED": "Chưa kết nối được. Bật Remote Debugging tại chrome://inspect/#remote-debugging trong Chrome đang mở, rồi cho phép kết nối.",
            "FACEBOOK_TAB_REQUIRED": "Tab Facebook đã đóng hoặc chuyển sang trang khác. Giữ tab Facebook mở rồi kết nối lại.",
            "SELECT_TAB": "Có nhiều tab Facebook. Chọn đúng tab bên trên rồi bấm Kết nối Chrome đang mở.",
            "OPENED_KEEP_BROWSER": "Đã mở nguồn trong tab Facebook đã chọn; Chrome và phiên đăng nhập vẫn mở.",
        }
        if status == "SUCCEEDED" and self.mode == "discover":
            counts = research_counts(body["candidates"])
            messages["SUCCEEDED"] = (
                f"Ứng viên {counts['candidates']}; đủ dữ liệu {counts['evaluated']}; đạt viral {counts['viral']}; {len(body['queries'])} truy vấn, {body['details_used']} lần mở chi tiết. Chọn/bỏ chọn trước khi Lưu; bài phân tích không được tính viral."
            )
        self.completed.emit(messages.get(status, messages["BROWSER_FAILED"]))
    except InterruptedError:
        self.completed.emit(
            f"Đã dừng. Giữ {saved} tư liệu đã lưu; dữ liệu cũ không đổi."
        )
    except Exception:
        self.completed.emit(
            "Không thể hoàn tất; dữ liệu đã lưu được giữ. Kiểm tra trình duyệt/dependencies và thử lại khi có thay đổi."
        )
    finally:
        pass
