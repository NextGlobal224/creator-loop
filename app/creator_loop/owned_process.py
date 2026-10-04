"""Windows process trees assigned to a private kill-on-close Job before execution.

Native handle ownership avoids PID reuse. No arbitrary PID termination API.
Win32 contracts: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import wintypes as w
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from creator_loop.windows_owned_file import _kernel32


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", w.DWORD),
        ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t),
        ("active", w.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_longlong)
        for name in ("user", "kernel", "period_user", "period_kernel")
    ] + [
        (name, w.DWORD)
        for name in (
            "page_faults",
            "total_processes",
            "active_processes",
            "terminated_processes",
        )
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("reserved", w.LPWSTR),
        ("desktop", w.LPWSTR),
        ("title", w.LPWSTR),
        ("x", w.DWORD),
        ("y", w.DWORD),
        ("x_size", w.DWORD),
        ("y_size", w.DWORD),
        ("x_chars", w.DWORD),
        ("y_chars", w.DWORD),
        ("fill", w.DWORD),
        ("flags", w.DWORD),
        ("show", w.WORD),
        ("reserved_size", w.WORD),
        ("reserved_bytes", ctypes.c_void_p),
        ("stdin", w.HANDLE),
        ("stdout", w.HANDLE),
        ("stderr", w.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", w.HANDLE),
        ("thread", w.HANDLE),
        ("pid", w.DWORD),
        ("tid", w.DWORD),
    ]


def _api() -> Any:
    if sys.platform != "win32" or os.name != "nt":
        raise OSError("Owned process trees require Windows")
    kernel = _kernel32()
    specs = {
        "CreateJobObjectW": ((ctypes.c_void_p, w.LPCWSTR), w.HANDLE),
        "SetInformationJobObject": (
            (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD),
            w.BOOL,
        ),
        "AssignProcessToJobObject": ((w.HANDLE, w.HANDLE), w.BOOL),
        "TerminateJobObject": ((w.HANDLE, w.UINT), w.BOOL),
        "QueryInformationJobObject": (
            (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p),
            w.BOOL,
        ),
        "TerminateProcess": ((w.HANDLE, w.UINT), w.BOOL),
        "WaitForSingleObject": ((w.HANDLE, w.DWORD), w.DWORD),
        "GetExitCodeProcess": ((w.HANDLE, ctypes.POINTER(w.DWORD)), w.BOOL),
        "ResumeThread": ((w.HANDLE,), w.DWORD),
        "GetProcessTimes": (
            (
                w.HANDLE,
                ctypes.POINTER(w.FILETIME),
                ctypes.POINTER(w.FILETIME),
                ctypes.POINTER(w.FILETIME),
                ctypes.POINTER(w.FILETIME),
            ),
            w.BOOL,
        ),
        "SetHandleInformation": ((w.HANDLE, w.DWORD, w.DWORD), w.BOOL),
        "GetCurrentProcess": ((), w.HANDLE),
        "GetCurrentThread": ((), w.HANDLE),
        "PeekNamedPipe": (
            (
                w.HANDLE,
                ctypes.c_void_p,
                w.DWORD,
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(w.DWORD),
            ),
            w.BOOL,
        ),
        "OpenProcess": ((w.DWORD, w.BOOL, w.DWORD), w.HANDLE),
        "QueryFullProcessImageNameW": (
            (w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)),
            w.BOOL,
        ),
        "DuplicateHandle": (
            (
                w.HANDLE,
                w.HANDLE,
                w.HANDLE,
                ctypes.POINTER(w.HANDLE),
                w.DWORD,
                w.BOOL,
                w.DWORD,
            ),
            w.BOOL,
        ),
        "InitializeProcThreadAttributeList": (
            (ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)),
            w.BOOL,
        ),
        "UpdateProcThreadAttribute": (
            (
                ctypes.c_void_p,
                w.DWORD,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ),
            w.BOOL,
        ),
        "DeleteProcThreadAttributeList": ((ctypes.c_void_p,), None),
        "CreateProcessW": (
            (
                w.LPCWSTR,
                w.LPWSTR,
                ctypes.c_void_p,
                ctypes.c_void_p,
                w.BOOL,
                w.DWORD,
                ctypes.c_void_p,
                w.LPCWSTR,
                ctypes.POINTER(_StartupInfo),
                ctypes.POINTER(_ProcessInfo),
            ),
            w.BOOL,
        ),
    }
    for name, (arguments, result) in specs.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int
    timed_out: bool
    elapsed_seconds: float


def _duplicate_read_input(kernel: Any, source: BinaryIO) -> BinaryIO:
    """Own a read-only duplicate; never close/change inheritance on the source.

    DuplicateHandle shares the kernel file position. The caller must keep the
    source open and avoid reading/seeking it until the child has been closed.
    https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-duplicatehandle
    """
    if sys.platform != "win32":
        raise OSError("Owned input handles require Windows")
    import msvcrt

    if not source.readable() or not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
        raise ValueError("Owned input requires a readable regular file")
    handle = w.HANDLE()
    current = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(
        current,
        msvcrt.get_osfhandle(source.fileno()),
        current,
        ctypes.byref(handle),
        0x80000000,
        False,
        0,  # GENERIC_READ only
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if handle.value is None:
        raise OSError("DuplicateHandle returned no input handle")
    try:
        fd = msvcrt.open_osfhandle(
            handle.value, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT
        )
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _duplicate_write_output(kernel: Any, source: BinaryIO) -> BinaryIO:
    """Own only a write duplicate; caller keeps its pipe/file and inheritance."""
    if sys.platform != "win32":
        raise OSError("Owned output handles require Windows")
    import msvcrt

    if not source.writable():
        raise ValueError("Owned output requires a writable binary stream")
    value = w.HANDLE()
    current = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(
        current,
        msvcrt.get_osfhandle(source.fileno()),
        current,
        ctypes.byref(value),
        0x40000000,
        False,
        0,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if value.value is None:
        raise OSError("DuplicateHandle returned no output handle")
    try:
        fd = msvcrt.open_osfhandle(
            value.value, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT
        )
    except BaseException:
        kernel.CloseHandle(value)
        raise
    try:
        return os.fdopen(fd, "wb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


class OwnedWindowsProcess:
    kernel: Any
    stdout_path: Path
    stderr_path: Path

    def __init__(
        self,
        executable: Path,
        arguments: list[str],
        log_directory: Path,
        *,
        component_version: str,
        environment: dict[str, str] | None = None,
        cwd: Path | None = None,
        capture_output: bool = True,
        stdin_source: BinaryIO | None = None,
        stdout_sink: BinaryIO | None = None,
        memory_limit_bytes: int | None = None,
        before_resume: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise OSError("Owned process trees require Windows")
        if (
            not isinstance(component_version, str)
            or not component_version
            or any(not isinstance(arg, str) or "\x00" in arg for arg in arguments)
        ):
            raise ValueError(
                "Valid component version and argument strings are required"
            )
        self.kernel = _api()
        if memory_limit_bytes is not None and (
            type(memory_limit_bytes) is not int
            or not 64 * 1024 * 1024 <= memory_limit_bytes <= 4 * 1024**3
        ):
            raise ValueError("Owned Job memory limit must be between 64 MiB and 4 GiB")
        self.executable = executable.resolve(strict=True)
        self.job: int | None = None
        self.process: int | None = None
        self.thread: int | None = None
        self.streams: list[BinaryIO] = []
        self.record: dict[str, object] = {}
        log_directory.mkdir()  # caller chooses a new owned log directory
        self.stdout_path = log_directory / "stdout.log"
        self.stderr_path = log_directory / "stderr.log"
        self.started = time.monotonic()
        try:
            self.job = self.kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if memory_limit_bytes is not None:
                limits.basic.flags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
                limits.job_memory = memory_limit_bytes
            if not self.kernel.SetInformationJobObject(
                self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self.streams.append(
                _duplicate_read_input(self.kernel, stdin_source)
                if stdin_source is not None
                else open(os.devnull, "rb")
            )
            self.streams.append(
                _duplicate_write_output(self.kernel, stdout_sink)
                if stdout_sink is not None
                else self.stdout_path.open("xb")
                if capture_output
                else open(os.devnull, "wb")
            )
            self.streams.append(
                self.stderr_path.open("xb")
                if capture_output
                else open(os.devnull, "wb")
            )
            self._create(arguments, environment, cwd, component_version, before_resume)
        except BaseException:
            self.close()
            raise

    def _create(
        self,
        arguments: list[str],
        environment: dict[str, str] | None,
        cwd: Path | None,
        component_version: str,
        before_resume: Callable[[dict[str, object]], None] | None,
    ) -> None:
        if sys.platform != "win32":
            raise OSError("Owned process trees require Windows")
        import msvcrt

        handles = (w.HANDLE * 3)(
            *(msvcrt.get_osfhandle(stream.fileno()) for stream in self.streams)
        )
        size = ctypes.c_size_t()
        self.kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        attributes = ctypes.cast(buffer, ctypes.c_void_p)
        if not self.kernel.InitializeProcThreadAttributeList(
            attributes, 1, 0, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            for handle in handles:
                if not self.kernel.SetHandleInformation(handle, 1, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.UpdateProcThreadAttribute(
                attributes,
                0,
                0x20002,
                ctypes.byref(handles),
                ctypes.sizeof(handles),
                None,
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            startup = _StartupInfoEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = (
                0x100  # STARTF_USESTDHANDLES; CREATE_NO_WINDOW below
            )
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = (
                handles
            )
            startup.attributes = attributes
            info = _ProcessInfo()
            command = ctypes.create_unicode_buffer(
                subprocess.list2cmdline([str(self.executable), *arguments])
            )
            env_buffer = None
            if environment is not None:
                if any(
                    not key or "=" in key or "\x00" in key or "\x00" in value
                    for key, value in environment.items()
                ):
                    raise ValueError("Invalid process environment")
                env_buffer = ctypes.create_unicode_buffer(
                    "\x00".join(
                        f"{key}={value}"
                        for key, value in sorted(
                            environment.items(), key=lambda entry: entry[0].casefold()
                        )
                    )
                    + "\x00\x00"
                )
            if not self.kernel.CreateProcessW(
                str(self.executable),
                command,
                None,
                None,
                True,
                0x4 | 0x400 | 0x80000 | 0x08000000,
                env_buffer,
                str(cwd.resolve(strict=True)) if cwd else None,
                ctypes.byref(startup.startup),
                ctypes.byref(info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self.process, self.thread = info.process, info.thread
            if not self.kernel.AssignProcessToJobObject(self.job, self.process):
                # Still suspended: this process cannot have launched any child.
                assignment_error = ctypes.get_last_error()
                self.kernel.TerminateProcess(self.process, 125)
                raise ctypes.WinError(assignment_error)
            times = [w.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(
                self.process, *(ctypes.byref(value) for value in times)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self.record = {
                "record_format": 1,
                "run_id": uuid4().hex,
                "job_id": uuid4().hex,
                "pid": info.pid,
                "executable": str(self.executable),
                "creation_identity": str(
                    (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                ),
                "creation_identity_format": "WIN32_FILETIME",
                "parent_pid": os.getpid(),
                "component_version": component_version,
            }
            # Recovery evidence exists before any owned code can run.
            with (self.stdout_path.parent / "ownership.json").open(
                "x", encoding="utf-8"
            ) as stream:
                json.dump(self.record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            if before_resume is not None:
                before_resume(self.record.copy())
            if self.kernel.ResumeThread(self.thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.DeleteProcThreadAttributeList(attributes)
            for handle in handles:
                self.kernel.SetHandleInformation(handle, 1, 0)
            if self.thread is not None:
                self.kernel.CloseHandle(self.thread)
                self.thread = None

    def wait(self, timeout_seconds: float) -> ProcessOutcome:
        if sys.platform != "win32":
            raise OSError("Owned process trees require Windows")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 86400:
            raise ValueError(
                "A finite process deadline between 0 and 86400s is required"
            )
        if self.process is None:
            raise RuntimeError("Owned process is closed")
        state = self.kernel.WaitForSingleObject(
            self.process, max(1, int(timeout_seconds * 1000))
        )
        timed_out = state == 0x102
        if state not in (0, 0x102):
            raise ctypes.WinError(ctypes.get_last_error())
        if timed_out:
            self.stop(124)
        code = w.DWORD()
        if not self.kernel.GetExitCodeProcess(self.process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.stop(code.value)  # cleanup descendants even if parent exited normally
        return ProcessOutcome(code.value, timed_out, time.monotonic() - self.started)

    def request_stop(self, exit_code: int = 125) -> None:
        """Terminate only this retained Job; do not wait on the GUI thread."""
        if sys.platform != "win32":
            raise OSError("Owned process trees require Windows")
        if self.job is not None:
            if not self.kernel.TerminateJobObject(self.job, exit_code):
                raise ctypes.WinError(ctypes.get_last_error())

    def tree_finished(self) -> bool:
        """Retained native handles prove both root exit and empty owned tree."""
        if sys.platform != "win32":
            raise OSError("Owned process trees require Windows")
        if self.job is None:
            return self.process is None
        accounting = _JobAccounting()
        if not self.kernel.QueryInformationJobObject(
            self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        state = (
            self.kernel.WaitForSingleObject(self.process, 0)
            if self.process is not None
            else 0
        )
        if state not in (0, 0x102):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active_processes == 0 and state == 0

    def stop(self, exit_code: int = 125) -> None:
        self.request_stop(exit_code)
        if self.job is not None:
            if self.process is not None:
                self.kernel.WaitForSingleObject(self.process, 5000)
            deadline = time.monotonic() + 5
            while not self.tree_finished():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Owned tree did not finish termination")
                time.sleep(0.01)

    def poll(self) -> ProcessOutcome | None:
        """Observe a desktop session without assigning a lifetime deadline."""
        if sys.platform != "win32":
            raise OSError("Owned process trees require Windows")
        if self.process is None:
            raise RuntimeError("Owned process is closed")
        state = self.kernel.WaitForSingleObject(self.process, 0)
        if state == 0x102:
            return None
        if state != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = w.DWORD()
        if not self.kernel.GetExitCodeProcess(self.process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.stop(code.value)
        return ProcessOutcome(code.value, False, time.monotonic() - self.started)

    def close(self) -> None:
        # Non-inherited Job handle: OS closing it on owner crash kills the tree.
        try:
            if self.job is not None:
                self.stop()
        finally:
            if self.job is not None:
                self.kernel.CloseHandle(self.job)
                self.job = None
            if self.process is not None:
                self.kernel.WaitForSingleObject(self.process, 5000)
                self.kernel.CloseHandle(self.process)
                self.process = None
            if self.thread is not None:
                self.kernel.CloseHandle(self.thread)
                self.thread = None
            for stream in self.streams:
                stream.close()
            self.streams.clear()

    def __enter__(self) -> OwnedWindowsProcess:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
