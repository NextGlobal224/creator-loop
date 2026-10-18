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
        headers = {"Origin": base, "Content-Type": "application/json"}
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
