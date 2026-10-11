"""Local-only DOM collection in a separately owned Chromium profile.

No Meta API, service, cookies, password values or page/network dumps are read.
The caller owns this worker and every driver/browser descendant in a private Job.
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import json
import os
import re
import time
from contextlib import ExitStack
from ctypes import wintypes as w
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from creator_loop.facebook_runtime import verified_browser, verify_browser_namespace
from creator_loop.facebook_sources import post_link
from creator_loop.owned_process import _api
from creator_loop.runtime_files import RuntimeHandle
from creator_loop.runtime_ownership import process_identity
from creator_loop.runtime_recovery import _identity_dead
from creator_loop.windows_paths import file_io_path, resolve_file_path

PLAYWRIGHT_VERSION = "1.63.0"
BROWSER_REVISION = "1243"
SCAN_SECONDS = 180
LOGIN_SECONDS = 600
OPEN_SECONDS = 600
# Adapted from AutoCLI facebook/search at c0969e2 (Apache-2.0).
# Unlike its first-anchor result, inspect all anchors and validate permalinks.
EXTRACT = r"""() => {
 const articles = [...document.querySelectorAll('[role="article"], [role="listitem"]')];
 const visible = e => !!(e.getClientRects().length && e.getBoundingClientRect().width);
 const truncate = (text, limit) => Array.from(text.trim()).slice(0,limit).join('');
 const out = [];
 for (const article of articles.filter(visible).slice(0,100)) {
   const text = truncate(article.innerText || '',8192);
   const publisher = truncate(article.querySelector('h2,h3,strong')?.innerText || '',200) || null;
   const message = article.querySelector('[data-ad-preview="message"], [data-ad-comet-preview="message"]');
   const anchors = [...article.querySelectorAll('a[href]')].filter(visible).slice(0,100);
   const primary = anchors.filter(a => a.closest('[role="article"], [role="listitem"]') === article && !message?.contains(a) && a.querySelector('abbr,time,[data-utime]'));
   const ids = [...new Set(primary.map(a=>a.href))];
   for (const a of anchors)
     out.push({url:a.href, excerpt:ids.length===1 && primary.includes(a) ? text || null : null, publisher:ids.length===1 && primary.includes(a) ? publisher : null});
 }
 // Reels/video cards sometimes have no article wrapper. Save URL without
 // fabricating a text/media file or unrelated ancestor text.
 for (const a of [...document.querySelectorAll('a[href]')].filter(visible).slice(0,1000))
   out.push({url:a.href, excerpt:null, publisher:null});
 return out.slice(0,2000);
}"""


def session_problem(page: Any) -> str | None:
    path = urlparse(page.url).path.lower()
    if any(x in path for x in ("/checkpoint", "/challenge", "/captcha")):
        return "CAPTCHA"
    if page.locator(
        'iframe[src*="captcha"], [id*="captcha"], [data-testid*="captcha"]'
    ).count():
        return "CAPTCHA"
    if (
        path.startswith("/login")
        or page.locator('input[type="password"]:visible').count()
    ):
        return "AUTH_REQUIRED"
    return None


def collect(
    page: Any,
    keyword: str,
    limit: int,
    emit: Callable[[dict[str, Any]], None],
    cancelled: Callable[[], bool],
    deadline: float,
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for section in ("posts", "videos"):
        if cancelled():
            raise InterruptedError()
        page.goto(
            "https://www.facebook.com/search/"
            + section
            + "/?q="
            + quote(keyword, safe=""),
            wait_until="domcontentloaded",
            timeout=20000,
        )
        page.wait_for_timeout(1500)
        idle = 0
        for turn in range(20):
            if cancelled():
                raise InterruptedError()
            if time.monotonic() >= deadline:
                raise TimeoutError()
            problem = session_problem(page)
            if problem:
                raise SessionRequired(problem)
            before = len(found)
            for item in page.evaluate(EXTRACT):
                link = post_link(item.get("url")) if isinstance(item, dict) else None
                if link is None or link.external_id in found:
                    continue
                found[link.external_id] = {
                    "url": link.url,
                    "excerpt": item.get("excerpt")
                    if isinstance(item.get("excerpt"), str)
                    else None,
                    "publisher": item.get("publisher")
                    if isinstance(item.get("publisher"), str)
                    else None,
                }
                if len(found) >= limit:
                    break
            emit(
                {
                    "status": "SCANNING",
                    "count": len(found),
                    "round": turn + 1,
                    "links": list(found.values()),
                }
            )
            if len(found) >= limit:
                return list(found.values())
            idle = idle + 1 if len(found) == before else 0
            if idle >= 4:
                break
            page.mouse.wheel(0, 850)
            page.wait_for_timeout(800)
    return list(found.values())


class SessionRequired(Exception):
    pass


def _validate_owner(marker: dict, child: dict, root: Path, workspace: Path) -> None:
    kernel = _api()
    observed = process_identity(kernel, kernel.GetCurrentProcess())
    component = "facebook-browser/1.63.0/chromium-1243"
    if (
        type(marker.get("format")) is not int
        or marker.get("format") != 1
        or marker.get("kind") != "FACEBOOK_BROWSER_WORKSPACE"
        or marker.get("data_root") != str(root)
        or marker.get("workspace") != workspace.name
        or marker.get("component_version") != component
        or not isinstance(marker.get("parent"), dict)
        or _identity_dead(marker["parent"])
        or type(child.get("record_format")) is not int
        or child.get("record_format") != 1
        or child.get("pid") != os.getpid()
        or child.get("parent_pid") != marker["parent"].get("pid")
        or child.get("component_version") != component
        or any(child.get(key) != value for key, value in observed.items())
        or any(
            not isinstance(child.get(key), str)
            or re.fullmatch("[0-9a-f]{32}", child[key]) is None
            for key in ("run_id", "job_id")
        )
    ):
        raise ValueError("Browser ownership identity mismatch")
    kernel.IsProcessInJob.argtypes = (w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL))
    kernel.IsProcessInJob.restype = w.BOOL
    in_job = w.BOOL()
    if (
        not kernel.IsProcessInJob(
            kernel.GetCurrentProcess(), None, ctypes.byref(in_job)
        )
        or not in_job.value
    ):
        raise OSError("Browser requires an owned Job")


def _request(path: Path, held: ExitStack) -> tuple[dict[str, Any], Path, Path]:
    root = resolve_file_path(path.parent.parent.parent, strict=True)
    workspace = path.parent
    run_id = workspace.name.removeprefix("facebook-")
    if (
        re.fullmatch("[0-9a-f]{32}", run_id) is None
        or path != root / "runtime" / ("facebook-" + run_id) / "request.json"
    ):
        raise ValueError("Invalid private browser request")
    for folder in (root, root / "runtime", workspace):
        held.enter_context(
            RuntimeHandle(folder, directory=True, allow_child_writes=True)
        )
    marker = held.enter_context(RuntimeHandle(workspace / "ownership.json")).read_json(
        reject_duplicates=True
    )
    child = held.enter_context(
        RuntimeHandle(workspace / "child-ownership.json")
    ).read_json(reject_duplicates=True)
    _validate_owner(marker, child, root, workspace)
    request = held.enter_context(RuntimeHandle(path, read_only=True)).read_json(
        reject_duplicates=True
    )
    if (
        set(request) != {"format", "mode", "keyword", "limit", "run_id", "url"}
        or type(request.get("format")) is not int
        or request.get("format") != 1
        or request.get("mode") not in ("scan", "login", "open")
        or not isinstance(request.get("keyword"), str)
        or len(request["keyword"]) > 200
        or (request["mode"] == "scan" and not request["keyword"].strip())
        or type(request.get("limit")) is not int
        or not 1 <= request["limit"] <= 100
        or request.get("run_id") != run_id
    ):
        raise ValueError("Invalid browser operation")
    profile = root / "facebook-browser-session"
    profile.mkdir(exist_ok=True)
    held.enter_context(RuntimeHandle(profile, directory=True, allow_child_writes=True))
    return request, workspace, profile


def _run_browser_task(path: Path, held: ExitStack) -> int:
    request, workspace, profile = _request(path, held)

    def emit(body: dict[str, Any]) -> None:
        body = {"format": 1, "run_id": request["run_id"], **body}
        target = workspace / "result.json"
        temp = workspace / "result.tmp"
        file_io_path(temp).write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(file_io_path(temp), file_io_path(target))

    def cancelled() -> bool:
        return (workspace / "stop").exists()

    mode = request["mode"]
    timeout = (
        SCAN_SECONDS
        if mode == "scan"
        else LOGIN_SECONDS
        if mode == "login"
        else OPEN_SECONDS
    )
    deadline = time.monotonic() + timeout
    final: dict[str, Any] = {}
    try:
        from playwright.sync_api import Error, sync_playwright

        if importlib.metadata.version("playwright") != PLAYWRIGHT_VERSION:
            raise ValueError("Pinned browser SDK required")

        root = workspace.parent.parent
        executable = verified_browser(root, held)
        emit({"status": "STARTING", "count": 0})
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(profile),
                executable_path=str(executable),
                headless=False,
                accept_downloads=False,
                timeout=30000,
                viewport={"width": 1180, "height": 800},
                args=["--disable-background-mode"],
                handle_sigint=False,
                handle_sigterm=False,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if mode == "scan":
                    links = collect(
                        page,
                        request["keyword"],
                        request["limit"],
                        emit,
                        cancelled,
                        deadline,
                    )
                    final = {"status": "SUCCEEDED", "count": len(links), "links": links}
                else:
                    url = (
                        "https://www.facebook.com/"
                        if mode == "login"
                        else request.get("url")
                    )
                    if mode == "open" and post_link(url) is None:
                        raise ValueError("Invalid saved post URL")
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    if mode == "open" and (problem := session_problem(page)):
                        raise SessionRequired(problem)
                    emit(
                        {
                            "status": "WAIT_LOGIN" if mode == "login" else "OPENED",
                            "count": 0,
                        }
                    )
                    while (
                        context.pages
                        and time.monotonic() < deadline
                        and not cancelled()
                    ):
                        if page.is_closed():
                            final = {"status": "CLOSED", "count": 0}
                            break
                        problem = session_problem(page)
                        if problem == "CAPTCHA" or (mode == "open" and problem):
                            raise SessionRequired(problem)
                        # User presses "session ready" in the app after direct login.
                        if mode == "login" and (workspace / "ready").exists():
                            problem = session_problem(page)
                            if problem:
                                raise SessionRequired(problem)
                            final = {"status": "SESSION_READY", "count": 0}
                            break
                        try:
                            page.wait_for_timeout(200)
                        except Error:
                            if page.is_closed() or not context.pages:
                                final = {"status": "CLOSED", "count": 0}
                                break
                            raise
                    else:
                        if cancelled():
                            raise InterruptedError()
                        if time.monotonic() >= deadline:
                            raise TimeoutError()
                        final = {"status": "CLOSED", "count": 0}
            finally:
                try:
                    context.close()
                except Error:
                    if final.get("status") != "CLOSED":
                        raise
        verify_browser_namespace(executable.parent)
        emit(final)
    except InterruptedError:
        emit({"status": "CANCELLED", "count": 0})
        return 2
    except SessionRequired as exc:
        emit({"status": str(exc), "count": 0})
        return 3
    except TimeoutError:
        emit({"status": "TIMED_OUT", "count": 0})
        return 4
    except Exception:
        # Never dump browser exceptions: they may contain URLs/session secrets.
        emit({"status": "BROWSER_FAILED", "count": 0})
        return 5
    return 0


def run_browser_task(path: Path) -> int:
    with ExitStack() as held:
        try:
            return _run_browser_task(path, held)
        except (OSError, ValueError, RuntimeError):
            return 6  # owner/request failure must never fabricate browser results
