"""Read Windows process identity without taking process-control actions."""

from __future__ import annotations

import json
import subprocess

from .worker_process import ProcessIdentity


def read_process_identity(pid: int) -> ProcessIdentity | None:
    """Return verified live identity data, or None when verification fails closed."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None

    script = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
        "if ($null -eq $p -or [string]::IsNullOrWhiteSpace($p.ExecutablePath) "
        "-or $null -eq $p.CreationDate) { exit 3 }; "
        "[ordered]@{ "
        "pid = [int]$p.ProcessId; "
        "executable = [string]$p.ExecutablePath; "
        "creation_identity = $p.CreationDate.ToUniversalTime().ToString('o') "
        "} | ConvertTo-Json -Compress"
    )

    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
            check=False,
        )

        if completed.returncode != 0:
            return None

        payload = json.loads(completed.stdout)

        actual_pid = payload.get("pid")
        executable = payload.get("executable")
        creation_identity = payload.get("creation_identity")

        if (
            isinstance(actual_pid, bool)
            or not isinstance(actual_pid, int)
            or actual_pid != pid
            or not isinstance(executable, str)
            or not executable.strip()
            or not isinstance(creation_identity, str)
            or not creation_identity.strip()
        ):
            return None

        return ProcessIdentity(
            pid=actual_pid,
            executable=executable,
            creation_identity=creation_identity,
        )
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return None
