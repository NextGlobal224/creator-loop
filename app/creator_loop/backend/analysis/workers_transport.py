"""One HTTPS request inside an owned child; no database, retry or fallback."""

import ctypes
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, cast

from creator_loop.backend.runtime.owned_process import (
    OwnedWindowsProcess,
    _api,
    _ExtendedLimits,
)
from creator_loop.backend.runtime.python_worker import python_worker
from creator_loop.backend.runtime.runtime_files import RuntimeHandle
from creator_loop.backend.runtime.runtime_ownership import (
    bind_workspace_child,
    process_identity,
)
from creator_loop.backend.runtime.runtime_recovery import _identity_dead

MAX_BYTES = 64 * 1024
QUALITY_REASONS = {
    "HYPOTHETICAL_LABEL_MISSING",
    "SOURCE_AUDIT_FAILED",
    "STAGE_OUTPUT_INVALID",
}


def save_quality_rejection(workspace: Path, status: int, raw: bytes, key: str) -> None:
    """Keep bounded selected-output diagnostics, never generic HTTP/error content."""
    if status not in (422, 502) or len(raw) > MAX_BYTES:
        return
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return
    if (
        not isinstance(value, dict)
        or value.get("error")
        != ("QUALITY_REJECTED" if status == 422 else "WORKERS_AI_FAILED")
        or not isinstance(value.get("reason"), str)
        or value.get("reason") not in QUALITY_REASONS
        or value.get("retryable") is not False
    ):
        return
    report = {k: value[k] for k in ("error", "reason", "retryable")}
    report["http_status"] = status
    stage = value.get("stage")
    if stage in (
        "prose",
        "editing",
        "annotation",
        "analysis",
        "writing",
        "source_audit",
    ):
        report["stage"] = stage
    output = value.get("stage_output")
    if isinstance(output, str) and len(output) <= 12000:
        report["stage_output"] = output
    body = value.get("candidate_body")
    if isinstance(body, str) and len(body) <= 12000:
        report["candidate_body"] = body
    issues = value.get("audit_issues")
    if (
        isinstance(issues, list)
        and len(issues) <= 40
        and all(isinstance(s, str) for s in issues)
    ):
        report["audit_issues"] = [s[:500] for s in issues]
    stages = value.get("stages")
    if isinstance(stages, list) and len(stages) <= 4:
        measured = []
        for item in stages:
            if (
                not isinstance(item, dict)
                or item.get("stage")
                not in ("analysis", "writing", "editing", "source_audit")
                or not isinstance(item.get("usage"), dict)
            ):
                continue
            usage = {
                name: number
                for name, number in item["usage"].items()
                if name
                in (
                    "input_tokens",
                    "output_tokens",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "neurons",
                )
                and type(number) in (int, float)
                and math.isfinite(number)
                and number >= 0
            }
            measured.append({"stage": item["stage"], "usage": usage})
        report["stages"] = measured
    # The app credential is never model input, but redact it defensively in diagnostics.
    text = json.dumps(report, ensure_ascii=False)
    if key:
        text = text.replace(key, "[REDACTED]")
    (workspace / "quality-rejection.json").write_text(text, encoding="utf-8")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ValueError("Worker redirect refused; credential remains scoped")


def post_owned(
    workspace: Path,
    marker: dict[str, object],
    endpoint: str,
    payload: bytes,
    key: str,
    cancelled: Callable[[], bool],
    *,
    timeout_seconds: float = 100,
) -> bytes:
    """Anonymous stdin keeps credentials out of argv, durable requests and logs."""
    if cancelled():
        raise InterruptedError("Cancelled before upload")
    executable, prefix, environment = python_worker(marker)
    response = workspace / "response.json"
    with tempfile.TemporaryFile() as source, response.open("wb") as target:
        source.write(
            json.dumps(
                {"endpoint": endpoint, "payload": payload.decode(), "key": key}
            ).encode()
        )
        source.seek(0)
        deadline = time.monotonic() + timeout_seconds
        with OwnedWindowsProcess(
            executable,
            [*prefix, "--creator-workers-http", str(workspace)],
            workspace / "process",
            component_version=str(marker["component_version"]),
            environment=environment,
            stdin_source=cast(BinaryIO, source),
            stdout_sink=target,
            memory_limit_bytes=256 * 1024**2,
            before_resume=lambda child: bind_workspace_child(workspace, marker, child),
        ) as process:
            while (outcome := process.poll()) is None:
                limits = _ExtendedLimits()
                if process.kernel.QueryInformationJobObject(
                    process.job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None
                ):
                    (workspace / "metrics.json").write_text(
                        json.dumps(
                            {
                                "peak_job_bytes": limits.peak_job,
                                "peak_process_bytes": limits.peak_process,
                                "memory_limit_bytes": 256 * 1024**2,
                            }
                        ),
                        encoding="utf-8",
                    )
                if cancelled():
                    raise InterruptedError(
                        "Cancelled; remote result unknown, no local Draft commit"
                    )
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Workers HTTP exceeded process deadline; no automatic retry"
                    )
                if response.stat().st_size > MAX_BYTES:
                    raise ValueError("Workers response exceeds budget")
                time.sleep(0.05)
            if outcome.exit_code != 0:
                diagnostic = workspace / "quality-rejection.json"
                reason = ""
                if diagnostic.is_file() and diagnostic.stat().st_size <= MAX_BYTES:
                    try:
                        report = json.loads(diagnostic.read_text(encoding="utf-8"))
                        if (
                            isinstance(report, dict)
                            and isinstance(report.get("reason"), str)
                            and report["reason"] in QUALITY_REASONS
                        ):
                            reason = "; " + str(report["reason"])
                    except (ValueError, OSError):
                        pass
                raise RuntimeError(
                    f"Workers HTTP child exited {outcome.exit_code}{reason}; no automatic retry"
                )
    raw = response.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("Workers response exceeds budget")
    return raw


def run_http(workspace: Path) -> int:
    try:
        directory = workspace.resolve(strict=True)
        with RuntimeHandle(directory / "ownership.json") as held:
            marker = held.read_json()
        with RuntimeHandle(directory / "child-ownership.json") as held:
            child = held.read_json()
        identity = process_identity(_api(), _api().GetCurrentProcess())
        if (
            marker.get("kind") != "CREATOR_LLM_WORKSPACE"
            or directory.parent != Path(marker["data_root"]) / "runtime"
            or directory.name != marker.get("workspace")
            or child.get("pid") != os.getpid()
            or child.get("component_version") != marker.get("component_version")
            or child.get("parent_pid") != marker["parent"]["pid"]
            or any(child.get(k) != v for k, v in identity.items())
            or _identity_dead(marker["parent"])
        ):
            return 3
        packet = json.loads(sys.stdin.buffer.read(128 * 1024 + 1))
        request = urllib.request.Request(
            packet["endpoint"],
            data=packet["payload"].encode(),
            headers={
                "Authorization": "Bearer " + packet["key"],
                "Content-Type": "application/json",
                "User-Agent": "CreatorLoopBeta/1.0 (Windows; Workers-AI-client)",
            },
            method="POST",
        )
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=90
        ) as reply:
            raw = reply.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return 4
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        return 0
    except urllib.error.HTTPError as error:
        save_quality_rejection(
            directory, error.code, error.read(MAX_BYTES + 1), packet["key"]
        )
        # Status only: never echo request, credential, content or remote exception.
        print(f"WORKERS_HTTP_{error.code}", file=sys.stderr)
        return 5
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        return 6
