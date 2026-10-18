"""Bounded independent HTTPS read/write probe; no inference or private DB export."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ORIGIN = "https://creator-loop-b-web.creator-loop-ai-beta.workers.dev"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "Redirect refused", headers, fp
        )


def browser_probe(targets: list[dict], password: str) -> dict:
    # The browser is isolated on CI; never use/copy the user's Chrome profile.
    # No trace/HAR/video, because those may capture authentication material.
    from playwright.sync_api import sync_playwright

    submitted_jobs = []
    screenshots = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})

        def guard(route):
            request = route.request
            if urlsplit(request.url).netloc != urlsplit(ORIGIN).netloc:
                route.abort()
            elif urlsplit(request.url).path == "/api/jobs" and request.method == "POST":
                submitted_jobs.append(True)
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        page = context.new_page()
        page.goto(ORIGIN + "/login.html")
        page.locator("#password").fill(password)
        page.locator("#login button").click()
        page.wait_for_url(ORIGIN + "/")
        for item in targets:
            page.wait_for_function(
                "id => [...document.querySelector('#project').options].some(o=>o.value===id)",
                arg=item["project_id"],
            )
            page.locator("#project").select_option(item["project_id"])
            page.locator("#workflow").select_option(item["workflow_id"])
            page.wait_for_function(
                "id => document.querySelector('#versions').value===id",
                arg=item["draft_version_id"],
            )
            assert (
                hashlib.sha256(
                    page.locator("#editor").input_value().encode()
                ).hexdigest()
                == item["body_sha256"]
            )
            assert (
                hashlib.sha256(
                    page.locator("#analysis").inner_text().encode()
                ).hexdigest()
                == item["analysis_sha256"]
            )
            assert "CHƯA DUYỆT" in page.locator(".status").inner_text()
            if item["format"] == "IMAGE_CAPTION":
                page.wait_for_function(
                    "document.querySelector('#image-preview').naturalWidth > 0"
                )
            with page.expect_response(
                lambda response: (
                    "/api/drafts" in response.url and response.request.method == "POST"
                )
            ) as response:
                page.locator("#save").click()
            assert response.value.status == 200
            saved = response.value.json()
            assert saved["draft_version_id"] == item["draft_version_id"]
            assert saved["version_no"] == item["version_no"]
            actual = page.request.get(
                ORIGIN + "/api/workflows/" + item["workflow_id"]
            ).json()
            assert len(actual["versions"]) == item["version_count"]
            current = max(actual["versions"], key=lambda version: version["version_no"])
            for field in (
                "draft_version_id",
                "version_no",
                "body_text",
                "direction",
                "image_file_id",
                "image_sha256",
                "image_provenance",
                "image_usage_note",
            ):
                assert current[field] == item["latest"][field]
            page.wait_for_function(
                "id => document.querySelector('#versions').value===id",
                arg=item["draft_version_id"],
            )
            screenshot = "b-web-" + item["mode"] + "-" + item["format"] + ".png"
            page.screenshot(path=screenshot, full_page=True)
            screenshots.append(screenshot)
        page.reload()
        item = targets[-1]
        page.wait_for_function(
            "id => [...document.querySelector('#project').options].some(o=>o.value===id)",
            arg=item["project_id"],
        )
        page.locator("#project").select_option(item["project_id"])
        page.locator("#workflow").select_option(item["workflow_id"])
        page.wait_for_function(
            "id => document.querySelector('#versions').value===id",
            arg=item["draft_version_id"],
        )
        assert (
            hashlib.sha256(page.locator("#editor").input_value().encode()).hexdigest()
            == item["body_sha256"]
        )
        context.close()
        browser.close()
    assert not submitted_jobs
    return {
        "status": "PASS",
        "actual_browser_login": True,
        "six_ui_reopen_noop_save": True,
        "reload_reopen": True,
        "screenshots": screenshots,
        "model_jobs_submitted": 0,
    }


def probe(base: str, expected: list[dict], password: str, deployment: str) -> dict:
    parsed = urlsplit(base)
    if (
        base != ORIGIN
        or parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".workers.dev")
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected a bare deployed workers.dev HTTPS origin")
    base = base.rstrip("/")
    cookie = ""
    opener = urllib.request.build_opener(NoRedirect())

    def call(path, data=None, authenticated=True):
        headers = {
            "Origin": base,
            "Content-Type": "application/json",
            "User-Agent": "CreatorLoop-B-Web-QA/1",
        }
        if authenticated:
            headers["Cookie"] = cookie
        if data is not None:
            headers["Idempotency-Key"] = uuid4().hex
        request = urllib.request.Request(
            base + path,
            data=None if data is None else json.dumps(data).encode(),
            headers=headers,
        )
        try:
            with opener.open(request, timeout=30) as response:
                # A redirect to a login page cannot count as API success.
                assert response.url == base + path
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def api(path, data=None):
        status, raw, _ = call(path, data)
        if status != 200:
            raise ValueError(f"Probe operation failed: {path}, HTTP {status}")
        return json.loads(raw)

    assert call("/api/state", authenticated=False)[0] == 401
    assert call("/media/not-a-real-file", authenticated=False)[0] == 401
    status, _, headers = call("/api/login", {"password": password}, authenticated=False)
    assert status == 200
    cookie = headers["Set-Cookie"].split(";")[0]
    assert all(
        flag in headers["Set-Cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/")
    )
    runtime = api("/api/runtime")
    assert runtime["components"]["protocol"] == "immutable-source-audit"
    assert runtime["deployment"] == deployment
    project = api("/api/projects", {"title": "Independent cloud availability probe"})
    raw = "Tư liệu kiểm tra đường truyền cô lập; không phải output AI.".encode()
    material = api(
        "/api/materials",
        {
            "display_name": "Independent isolated probe",
            "media_type": "TEXT",
            "content_base64": base64.b64encode(raw).decode(),
            "source": {"publisher": "Creator Loop QA", "rights": "OWNED"},
        },
    )
    assert call("/media/" + material["file_id"])[1] == raw
    state = api("/api/state")
    assert any(p["project_id"] == project["project_id"] for p in state["projects"])
    results = []
    browser_targets = []
    if len(expected) != 6:
        raise ValueError("All six deployed mode/format outputs are required")
    pairs = set()
    for item in expected:
        workflow = api("/api/workflows/" + item["workflow_id"])
        pair = (workflow["mode"], workflow["output_format"])
        assert pair not in pairs
        pairs.add(pair)
        latest = max(workflow["versions"], key=lambda version: version["version_no"])
        actual = hashlib.sha256(latest["body_text"].encode()).hexdigest()
        assert actual == item["body_sha256"]
        assert latest["draft_version_id"] == item["draft_version_id"]
        if pair[1] == "IMAGE_CAPTION":
            status, image, _ = call("/media/" + latest["image_file_id"])
            assert (
                status == 200
                and hashlib.sha256(image).hexdigest() == item["image_sha256"]
            )
        results.append({**item, "mode": pair[0], "format": pair[1], "reopened": True})
        browser_targets.append(
            {
                **results[-1],
                "project_id": workflow["project_id"],
                "version_no": latest["version_no"],
                "version_count": len(workflow["versions"]),
                "latest": latest,
                "analysis_sha256": hashlib.sha256(
                    workflow["analysis_text"].encode()
                ).hexdigest(),
            }
        )
    assert pairs == {
        (mode, form)
        for mode in ("AUTOMATIC", "MANUAL")
        for form in ("TEXT", "IMAGE_CAPTION", "VIDEO")
    }
    return {
        "format": "B_WEB_INDEPENDENT_V1",
        "observed_at": time.time(),
        "origin": base,
        "environment": "GitHub-hosted Ubuntu",
        "runtime": runtime,
        "isolated_cloud_write_reopen": True,
        "unauthenticated_api_media_denied": True,
        "windows_localhost_tunnel_used": False,
        "model_calls": 0,
        "flows": results,
        "browser": browser_probe(browser_targets, password),
        "status": "PASS",
    }


if __name__ == "__main__":
    result = probe(
        os.environ["B_WEB_ORIGIN"],
        json.loads(os.environ["B_WEB_EXPECTED"]),
        os.environ["B_WEB_QA_PASSWORD"],
        os.environ["B_WEB_DEPLOYMENT"],
    )
    Path("b-web-independent.json").write_text(json.dumps(result, indent=2), "utf-8")
    print("Independent cloud storage/auth/six-output reopen PASS; zero model calls")
