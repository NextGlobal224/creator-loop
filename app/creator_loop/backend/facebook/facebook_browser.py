"""Local-only collection in an owned profile or an attached Facebook tab.

No Meta API, service, cookies, password values or page/network dumps are read.
The caller owns this worker and every driver/browser descendant in a private Job.
"""

from __future__ import annotations

import ctypes
import hashlib
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

from creator_loop.backend.facebook.facebook_cdp import (
    ConnectionRequired,
    is_facebook_url,
)
from creator_loop.backend.facebook.facebook_runtime import (
    verified_browser,
    verify_browser_namespace,
)
from creator_loop.backend.facebook.facebook_sources import post_link
from creator_loop.backend.runtime.owned_process import _api
from creator_loop.backend.runtime.runtime_files import RuntimeHandle
from creator_loop.backend.runtime.runtime_ownership import process_identity
from creator_loop.backend.runtime.runtime_recovery import _identity_dead
from creator_loop.backend.runtime.windows_paths import file_io_path, resolve_file_path

PLAYWRIGHT_VERSION = "1.63.0"
BROWSER_REVISION = "1243"
SCAN_SECONDS = 180
LOGIN_SECONDS = 600
OPEN_SECONDS = 600
# Adapted from AutoCLI facebook/search at c0969e2 (Apache-2.0).
# Unlike its first-anchor result, inspect all anchors and validate permalinks.
EXTRACT = r"""() => {
 if (location.protocol !== 'https:' || location.port || !['facebook.com','www.facebook.com','m.facebook.com','web.facebook.com'].includes(location.hostname)) return [];
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

# Facebook search timestamps can be obfuscated links pointing back to the
# search page. Their real permalink is supplied only when opened normally.
LAZY_TIMESTAMPS = r"""() => {
 if (location.protocol !== 'https:' || !['facebook.com','www.facebook.com','m.facebook.com','web.facebook.com'].includes(location.hostname) || !['/search/posts/','/search/videos/'].includes(location.pathname)) return [];
 const main=document.querySelector('[role="main"]');
 if (!main) return [];
 return [...document.querySelectorAll('a[href]')].map((a,index)=>({a,index})).filter(({a})=>{
   if (!main.contains(a)||!a.getClientRects().length||!a.getBoundingClientRect().width) return false;
   try {const u=new URL(a.href);return u.origin===location.origin&&u.pathname===location.pathname&&u.searchParams.has('__cft__[0]');}catch{return false;}
 }).slice(0,40).map(({a,index})=>({index,href:a.href}));
}"""


def resolve_search_timestamps(
    page: Any,
    found: dict[str, dict[str, Any]],
    seen: set[str],
    limit: int,
    cancelled: Callable[[], bool],
    deadline: float,
    scope_check: Callable[[], bool] | None,
) -> None:
    for candidate in page.evaluate(LAZY_TIMESTAMPS):
        if cancelled():
            raise InterruptedError()
        if time.monotonic() >= deadline:
            raise TimeoutError()
        if scope_check is not None and not scope_check():
            raise SessionRequired("FACEBOOK_TAB_REQUIRED")
        if (
            not isinstance(candidate, dict)
            or type(candidate.get("index")) is not int
            or not 0 <= candidate["index"] <= 10000
            or not isinstance(candidate.get("href"), str)
            or len(candidate["href"]) > 4096
        ):
            raise ValueError("Invalid timestamp candidate")
        key = hashlib.sha256(candidate["href"].encode("utf-8")).hexdigest()
        if key in seen:
            continue
        element = page.locator("a[href]").nth(candidate["index"])
        # Re-observe the same element after every SPA transition. Never use a
        # stale index for input or click a profile/like/share control.
        if element.evaluate("a=>a.href") != candidate["href"]:
            break
        previous = page.url
        seen.add(key)
        element.click(timeout=5000)
        page.wait_for_timeout(700)
        if cancelled():
            raise InterruptedError()
        if not is_facebook_url(page.url) or (
            scope_check is not None and not scope_check()
        ):
            raise SessionRequired("FACEBOOK_TAB_REQUIRED")
        if problem := session_problem(page):
            raise SessionRequired(problem)
        link = post_link(page.url)
        if link is not None:
            found.setdefault(
                link.external_id, {"url": link.url, "excerpt": None, "publisher": None}
            )
        if page.url != previous:
            page.go_back(wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(400)
        if len(found) >= limit:
            return
        # History can rebuild/virtualize the card DOM; take fresh candidates
        # on the next collection round rather than reusing old element indexes.
        break


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
    scope_check: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    seen_timestamps: set[str] = set()
    for section in ("posts", "videos"):
        if cancelled():
            raise InterruptedError()
        if scope_check is not None and not scope_check():
            raise SessionRequired("FACEBOOK_TAB_REQUIRED")
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
            if scope_check is not None and not scope_check():
                raise SessionRequired("FACEBOOK_TAB_REQUIRED")
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
            if len(found) < limit:
                resolve_search_timestamps(
                    page,
                    found,
                    seen_timestamps,
                    limit,
                    cancelled,
                    deadline,
                    scope_check,
                )
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


def _validate_owner(
    marker: dict, child: dict, root: Path, workspace: Path, component: str
) -> None:
    kernel = _api()
    observed = process_identity(kernel, kernel.GetCurrentProcess())
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


def _request(path: Path, held: ExitStack) -> tuple[dict[str, Any], Path, Path | None]:
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
    request = held.enter_context(RuntimeHandle(path, read_only=True)).read_json(
        reject_duplicates=True
    )
    research = request.pop("criteria", None)
    from creator_loop.backend.facebook.facebook_research import MAX_SCAN_BUDGETS

    if (
        set(request)
        not in (
            {"format", "mode", "keyword", "limit", "run_id", "url"},
            {
                "format",
                "mode",
                "keyword",
                "limit",
                "run_id",
                "url",
                "transport",
                "target_id",
            },
        )
        or type(request.get("format")) is not int
        or request.get("format") != 1
        or request.get("mode") not in ("scan", "discover", "login", "open", "connect")
        or not isinstance(request.get("keyword"), str)
        or len(request["keyword"]) > 200
        or (request["mode"] == "scan" and not request["keyword"].strip())
        or type(request.get("limit")) is not int
        or not 1
        <= request["limit"]
        <= (
            MAX_SCAN_BUDGETS["max_candidates"]
            if request.get("mode") == "discover"
            else 100
        )
        or request.get("run_id") != run_id
    ):
        raise ValueError("Invalid browser operation")
    if request["mode"] == "discover":
        from creator_loop.backend.facebook.facebook_research import criteria

        request["criteria"] = criteria(research)
        if request["limit"] != request["criteria"]["max_candidates"]:
            raise ValueError("Candidate budget mismatch")
    elif research is not None:
        raise ValueError("Unexpected research criteria")
    transport = request.get("transport", "private")
    if transport not in ("private", "existing"):
        raise ValueError("Invalid browser transport")
    component = "facebook-browser/1.63.0/" + (
        "existing-cdp" if transport == "existing" else "chromium-1243"
    )
    _validate_owner(marker, child, root, workspace, component)
    if transport == "existing":
        from creator_loop.backend.facebook.facebook_cdp import TARGET_ID

        target = request.get("target_id")
        if (
            request["mode"] == "login"
            or (
                target is not None
                and (not isinstance(target, str) or not TARGET_ID.fullmatch(target))
            )
            or (request["mode"] in ("scan", "discover", "open") and target is None)
        ):
            raise ValueError("Explicit existing Facebook tab required")
        return request, workspace, None
    if request["mode"] == "connect" or request.get("target_id") is not None:
        raise ValueError("Invalid private operation")
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
        600
        if mode == "discover"
        else SCAN_SECONDS
        if mode == "scan"
        else LOGIN_SECONDS
        if mode in ("login", "connect")
        else OPEN_SECONDS
    )
    deadline = time.monotonic() + timeout
    final: dict[str, Any] = {}
    try:
        from playwright.sync_api import Error, sync_playwright

        from creator_loop.backend.facebook.facebook_cdp import attached_operation

        if importlib.metadata.version("playwright") != PLAYWRIGHT_VERSION:
            raise ValueError("Pinned browser SDK required")

        root = workspace.parent.parent
        if profile is None:
            with sync_playwright() as pw:
                final = attached_operation(pw, request, emit, cancelled, deadline)
            emit(final)
            return 8 if final["status"] == "FACEBOOK_TAB_REQUIRED" else 0
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
                if mode == "discover":
                    from creator_loop.backend.facebook.facebook_discovery import (
                        discover,
                    )

                    final = discover(
                        page, request["criteria"], emit, cancelled, deadline
                    )
                    emit(final)
                elif mode == "scan":
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
                    if not isinstance(url, str) or (
                        mode == "open" and post_link(url) is None
                    ):
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
        return 8 if str(exc) == "FACEBOOK_TAB_REQUIRED" else 3
    except ConnectionRequired:
        emit({"status": "CONNECTION_REQUIRED", "count": 0})
        return 7
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
