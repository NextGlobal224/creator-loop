"""Attach to a user-owned Chrome session; never launch or close that browser."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from creator_loop.facebook_sources import HOSTS, post_link

TARGET_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
CONNECTION_SECONDS = 120


class ConnectionRequired(Exception):
    pass


def chrome_endpoint(descriptor: Path | None = None) -> str:
    """Read only Chrome's local DevTools descriptor, never its session files."""
    if descriptor is None:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise ConnectionRequired()
        descriptor = Path(local) / "Google/Chrome/User Data/DevToolsActivePort"
    try:
        if (
            descriptor.is_symlink()
            or descriptor.is_junction()
            or not descriptor.is_file()
        ):
            raise ConnectionRequired()
        if descriptor.stat().st_size > 4096:
            raise ConnectionRequired()
        lines = descriptor.read_text(encoding="utf-8").strip().splitlines()
        if (
            len(lines) != 2
            or re.fullmatch(r"[0-9]{1,5}", lines[0]) is None
            or not 1 <= int(lines[0]) <= 65535
            or re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{8,128}", lines[1]) is None
        ):
            raise ConnectionRequired()
        # Do not persist or log the websocket capability/path.
        return "ws://127.0.0.1:" + lines[0] + lines[1]
    except (OSError, UnicodeError):
        raise ConnectionRequired() from None


def is_facebook_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlparse(value)
        return (
            url.scheme == "https"
            and url.hostname in HOSTS
            and not url.username
            and not url.password
            and url.port is None
        )
    except ValueError:
        return False


def facebook_tabs(browser: Any) -> list[tuple[str, Any, str]]:
    tabs = []
    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed() or not is_facebook_url(page.url):
                continue
            session = context.new_cdp_session(page)
            try:
                info = session.send("Target.getTargetInfo")["targetInfo"]
            finally:
                session.detach()
            target = info.get("targetId")
            if (
                info.get("type") == "page"
                and is_facebook_url(info.get("url"))
                and isinstance(target, str)
                and TARGET_ID.fullmatch(target)
            ):
                tabs.append((target, page, page.title()[:200]))
            if len(tabs) > 50:
                raise ConnectionRequired()
    return tabs


def attached_operation(
    playwright: Any,
    request: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    cancelled: Callable[[], bool],
    deadline: float,
) -> dict[str, Any]:
    from creator_loop.facebook_browser import SessionRequired, collect, session_problem

    if cancelled():
        raise InterruptedError()
    emit({"status": "CONNECTING", "count": 0})
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    browser = playwright.chromium.connect_over_cdp(
        chrome_endpoint(),
        timeout=max(1, int(min(CONNECTION_SECONDS, remaining) * 1000)),
        no_defaults=True,
    )
    # The Playwright context manager disconnects the owned driver on exit.
    # Never call browser.close(), context.close(), page.close(), new_context()
    # or new_page(): every existing tab/process belongs to the user.
    tabs = facebook_tabs(browser)
    if cancelled():
        raise InterruptedError()
    target = request.get("target_id")
    selected = [item for item in tabs if item[0] == target] if target else tabs
    if not selected:
        return {"status": "FACEBOOK_TAB_REQUIRED", "count": 0}
    if len(selected) != 1:
        return {
            "status": "SELECT_TAB",
            "count": 0,
            "tabs": [{"target_id": tid, "title": title} for tid, _, title in tabs],
        }
    target, page, _ = selected[0]
    if not is_facebook_url(page.url):
        return {"status": "FACEBOOK_TAB_REQUIRED", "count": 0}
    problem = session_problem(page)
    if problem:
        raise SessionRequired(problem)
    if cancelled():
        raise InterruptedError()
    mode = request["mode"]
    if mode == "connect":
        account = page.locator(
            '[role="button"][aria-label="Account"]:visible, '
            '[role="button"][aria-label="Tài khoản"]:visible, '
            '[role="button"][aria-label="Your profile"]:visible, '
            '[role="button"][aria-label="Trang cá nhân của bạn"]:visible'
        )
        if account.count() == 0:
            return {"status": "SESSION_UNCONFIRMED", "count": 0}
        return {"status": "SESSION_READY", "count": 0, "target_id": target}
    if mode == "open":
        url = request.get("url")
        link = post_link(url)
        if link is None or link.url != url:
            raise ValueError("Invalid saved post")
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if not is_facebook_url(page.url):
            return {"status": "FACEBOOK_TAB_REQUIRED", "count": 0}
        if problem := session_problem(page):
            raise SessionRequired(problem)
        return {"status": "OPENED_KEEP_BROWSER", "count": 0}
    if mode != "scan" or target != request.get("target_id"):
        raise ConnectionRequired()
    links = collect(
        page,
        request["keyword"],
        request["limit"],
        emit,
        cancelled,
        deadline,
        scope_check=lambda: is_facebook_url(page.url) and not page.is_closed(),
    )
    return {"status": "SUCCEEDED", "count": len(links), "links": links}
