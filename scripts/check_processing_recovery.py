"""Exact EXE startup reconciliation and read-only smoke modes on fixture data."""

import argparse
import ctypes
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.database import initialize
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.paths import ensure_data_root
from creator_loop.runtime_ownership import create_workspace_marker


def seed_runtime(root: Path, environment: dict[str, str]) -> tuple[Path, Path, Path]:
    """A real short-lived owner binds a terminal child; live/unbound entries stay."""
    dead = root / "runtime/decode-dead0001"
    dead.mkdir()
    code = """import sys
from pathlib import Path
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.runtime_ownership import create_workspace_marker,bind_workspace_child
root=Path(sys.argv[1]); work=Path(sys.argv[2]); component='qt-decoder/recovery-probe/fixture'
marker=create_workspace_marker(root,work,component)
(work/'request.json').write_text('{}')
with OwnedWindowsProcess(Path(sys.executable),['-c','pass'],root/'logs'/'runtime-fixture',
    component_version=component,before_resume=lambda info:bind_workspace_child(work,marker,info)) as child:
    outcome=child.wait(10)
    assert outcome.exit_code==0 and not outcome.timed_out
(work/'response.json').write_text('{}')
(work/'frame.png').write_bytes(b'private synthetic runtime payload')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root), str(dead)],
        env=environment,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Runtime fixture owner failed: {result.stderr!r}")
    live = root / "runtime/decode-live0001"
    live.mkdir()
    create_workspace_marker(root, live, "qt-decoder/recovery-probe/fixture")
    unknown = root / "runtime/decode-unkn0001"
    unknown.mkdir()
    (unknown / "unowned-file").write_bytes(b"preserve unknown runtime")
    return dead, live, unknown


def close_owned_windows(process: OwnedWindowsProcess) -> int:
    """Post WM_CLOSE only to visible windows in our retained private Job.

    https://learn.microsoft.com/en-us/windows/win32/api/jobapi/nf-jobapi-isprocessinjob
    No lookup/terminate from a stale PID record; venv/EXE child members count.
    """
    kernel = process.kernel
    kernel.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    kernel.OpenProcess.restype = w.HANDLE
    kernel.IsProcessInJob.argtypes = (w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL))
    kernel.IsProcessInJob.restype = w.BOOL
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = (callback_type, w.LPARAM)
    user.EnumWindows.restype = w.BOOL
    user.GetWindowThreadProcessId.argtypes = (w.HWND, ctypes.POINTER(w.DWORD))
    user.GetWindowThreadProcessId.restype = w.DWORD
    user.IsWindowVisible.argtypes = (w.HWND,)
    user.IsWindowVisible.restype = w.BOOL
    user.PostMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    user.PostMessageW.restype = w.BOOL
    count = [0]

    @callback_type
    def visit(window, _parameter):
        if not user.IsWindowVisible(window):
            return True
        pid = w.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                member = w.BOOL()
                if (
                    kernel.IsProcessInJob(handle, process.job, ctypes.byref(member))
                    and member.value
                ):
                    if user.PostMessageW(window, 0x10, 0, 0):  # WM_CLOSE
                        count[0] += 1
            finally:
                kernel.CloseHandle(handle)
        return True

    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return count[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--component-version", required=True)
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    args.work_root.mkdir()
    root = args.work_root.resolve(strict=True) / "Synthetic user data Hue space"
    ensure_data_root(root)
    path = root / "creator_loop.sqlite3"
    initialize(path)
    fixture = (
        Path(__file__).resolve().parents[1] / "tests/fixtures/recovery-image.png"
    ).read_bytes()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO assets(asset_id,media_type,display_name,created_at) VALUES('image','IMAGE','Synthetic recovery','2026-10-03T00:00:00Z')"
        )
        db.execute(
            """INSERT INTO asset_files(file_id,asset_id,role,storage_key,sha256,byte_size,mime_type,created_at)
            VALUES('original','image','ORIGINAL','storage/originals/fixture.png',?,?,'image/png','2026-10-03T00:00:00Z')""",
            (hashlib.sha256(fixture).hexdigest(), len(fixture)),
        )
        for run_id, status, tool in (
            ("interrupted", "RUNNING", "PySide6 Qt image scaler"),
            ("terminal", "SUCCEEDED", "PySide6 Qt image scaler"),
            ("queued", "QUEUED", "PySide6 Qt image scaler"),
            ("unknown", "RUNNING", "unselected external executor"),
        ):
            db.execute(
                """INSERT INTO processing_runs(run_id,asset_id,input_file_id,task_type,status,tool_name,tool_version,started_at,finished_at,created_at)
                VALUES(?,'image','original','IMAGE_THUMBNAIL',?,?, 'legacy-fixture','2026-10-03T00:00:00Z',?,'2026-10-03T00:00:00Z')""",
                (
                    run_id,
                    status,
                    tool,
                    "2026-10-03T00:00:01Z" if status == "SUCCEEDED" else None,
                ),
            )
    original = root / "storage/originals/fixture.png"
    original.write_bytes(fixture)
    env = os.environ.copy()
    env["CREATOR_LOOP_DATA_ROOT"] = str(root.resolve())
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
    prefix = ["-m", "creator_loop"] if args.source else []
    dead, live, unknown = seed_runtime(root.resolve(), env)

    def runtime_snapshot():
        return {
            str(file.relative_to(root)): file.read_bytes()
            for directory in (dead, live, unknown)
            if directory.exists()
            for file in directory.iterdir()
        }

    runtime_before = runtime_snapshot()

    def snapshot():
        with sqlite3.connect(path) as db:
            return list(db.execute("SELECT * FROM processing_runs ORDER BY run_id"))

    before = snapshot()
    for index, mode in enumerate(
        (
            ["--smoke"],
            ["--compatible-only", "--smoke"],
            ["--ui-smoke"],
            ["--compatible-only", "--ui-smoke"],
        )
    ):
        env["QT_QPA_PLATFORM"] = "offscreen"
        with OwnedWindowsProcess(
            args.executable,
            [*prefix, *mode],
            args.work_root / f"smoke-{index}",
            component_version=args.component_version,
            environment=env,
        ) as child:
            outcome = child.wait(15)
        if (
            outcome.timed_out
            or outcome.exit_code != 0
            or snapshot() != before
            or runtime_snapshot() != runtime_before
        ):
            raise RuntimeError(
                f"Exact smoke {mode} altered run history or failed: {outcome}"
            )
    env["QT_QPA_PLATFORM"] = "windows"
    with OwnedWindowsProcess(
        args.executable,
        [*prefix, "--compatible-only"],
        args.work_root / "normal-ui",
        component_version=args.component_version,
        environment=env,
        capture_output=False,
    ) as child:
        deadline = time.monotonic() + 30
        close_count = 0
        while time.monotonic() < deadline:
            outcome = child.poll()
            if outcome is not None:
                break
            with sqlite3.connect(path) as db:
                status = db.execute(
                    "SELECT status FROM processing_runs WHERE run_id='interrupted'"
                ).fetchone()[0]
            if status == "FAILED" and not dead.exists():
                close_count += close_owned_windows(child)
            time.sleep(0.1)
        else:
            raise TimeoutError("Exact normal UI did not reconcile and close within30s")
        if outcome.exit_code != 0 or close_count == 0:
            raise RuntimeError(f"Exact owned UI failed to close normally: {outcome}")
    after = {row[0]: row for row in snapshot()}
    expected_runtime = {
        key: value
        for key, value in runtime_before.items()
        if not key.startswith(f"runtime/{dead.name}/")
        and not key.startswith(f"runtime\\{dead.name}\\")
    }
    if dead.exists() or runtime_snapshot() != expected_runtime:
        raise RuntimeError("Exact runtime cleanup altered live/unbound workspace")
    if not (root / "logs/runtime-fixture/ownership.json").exists():
        raise RuntimeError("Exact runtime cleanup removed ownership logs")
    for row in before:
        if row[0] != "interrupted" and after[row[0]] != row:
            raise RuntimeError("Exact recovery changed queued/unknown/terminal history")
    with sqlite3.connect(path) as db:
        recovered = db.execute(
            "SELECT status,finished_at,error_code,tool_version FROM processing_runs WHERE run_id='interrupted'"
        ).fetchone()
    if (
        recovered[0] != "FAILED"
        or not recovered[1]
        or recovered[2:] != ("INTERRUPTED_AT_STARTUP", "legacy-fixture")
        or original.read_bytes() != fixture
    ):
        raise RuntimeError(
            "Exact startup recovery lacks preserved original/provenance or terminal evidence"
        )
    print(
        "Exact UI processing/runtime recovery + unchanged smoke/history/live/unbound/original/logs PASS"
    )


if __name__ == "__main__":
    main()
