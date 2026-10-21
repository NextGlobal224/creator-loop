"""Additional creation-time proof for private preservation, never recovery."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from creator_loop.backend.runtime.owned_process import _api
from creator_loop.backend.runtime.runtime_ownership import process_identity
from creator_loop.backend.runtime.runtime_recovery import _identity_dead


def _powershell() -> Path:
    directory = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    function = directory.GetSystemDirectoryW
    function.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    function.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("Windows system directory unavailable")
    return Path(buffer.value) / "WindowsPowerShell/v1.0/powershell.exe"


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate process evidence key")
        result[key] = value
    return result


def _later_local_creation(identity: dict[str, Any]) -> bool:
    """Fresh local CIM creation proof, calibrated against this native process.

    Win32_Process.CreationDate is documented for detecting reused parent PIDs.
    A later, disjoint timestamp proves the original process cannot still exist;
    a name, missing executable, rounded inequality or missing row never does.
    https://learn.microsoft.com/windows/win32/cimwin32prov/win32-process
    """
    target, current = identity["pid"], os.getpid()
    if target == current:
        return False
    kernel = _api()
    witness = process_identity(kernel, kernel.GetCurrentProcess())
    executable = _powershell()
    modules = executable.parent / "Modules"

    def literal(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    # No remote session, arbitrary command, profile, names or command lines.
    # Pin module implementations as well as the executable: -NoProfile alone
    # does not prevent inherited PSModulePath from shadowing automatic imports.
    command = (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        "$PSModuleAutoLoadingPreference='None';"
        "Microsoft.PowerShell.Core\\Import-Module -Name "
        + literal(
            modules / "Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1"
        )
        + ";Microsoft.PowerShell.Core\\Import-Module -Name "
        + literal(modules / "CimCmdlets/CimCmdlets.psd1")
        + ";$rows=@(CimCmdlets\\Get-CimInstance -Namespace root/cimv2 -ClassName Win32_Process "
        f"-Filter 'ProcessId={target} OR ProcessId={current}' "
        "-Property ProcessId,CreationDate -OperationTimeoutSec 5 | "
        "Microsoft.PowerShell.Utility\\Select-Object -First 3);"
        "$proof=@($rows|Microsoft.PowerShell.Core\\ForEach-Object {"
        "if($_.CreationDate -isnot [datetime] -or "
        "$_.CreationDate.Kind -eq [DateTimeKind]::Unspecified){throw 'Ambiguous date'};"
        "[pscustomobject]@{pid=[uint32]$_.ProcessId;"
        "creation=$_.CreationDate.ToFileTimeUtc().ToString("
        "[Globalization.CultureInfo]::InvariantCulture)}});"
        "Microsoft.PowerShell.Utility\\ConvertTo-Json -Compress -InputObject $proof"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() != "psmodulepath"
    }
    environment["PSModulePath"] = str(modules)
    result = subprocess.run(
        [
            str(executable),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=12,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=environment,
    )
    if len(result.stdout.encode("utf-8")) > 4096 or result.stderr.strip():
        raise ValueError("Unexpected process evidence output")
    rows = json.loads(result.stdout, object_pairs_hook=_unique)
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError("Incomplete process evidence")
    observed: dict[int, int] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"pid", "creation"}
            or type(row["pid"]) is not int
            or row["pid"] not in {target, current}
            or row["pid"] in observed
            or not isinstance(row["creation"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", row["creation"])
        ):
            raise ValueError("Malformed process identity evidence")
        observed[row["pid"]] = int(row["creation"])
    if set(observed) != {target, current}:
        raise ValueError("Missing native process witness")
    now = 116444736000000000 + time.time_ns() // 100
    if any(not 0 < value <= now for value in observed.values()):
        raise ValueError("Implausible creation timestamp")
    # CIM dates have microsecond precision. Native witness must agree to 1ms;
    # target must be over one full second later, never just unequal/rounded.
    native_creation = witness["creation_identity"]
    if not isinstance(native_creation, str) or not re.fullmatch(
        r"[1-9][0-9]{0,19}", native_creation
    ):
        raise ValueError("Malformed native creation witness")
    if abs(observed[current] - int(native_creation)) > 10000:
        raise ValueError("Provider/native creation witness mismatch")
    original = int(identity["creation_identity"])
    return 0 < original < observed[target] - 10000000


def preservation_identity_dead(identity: dict[str, Any]) -> bool:
    """Never used by ordinary backup, cleanup, recovery, or termination."""
    try:
        return _identity_dead(identity)
    except OSError as original:
        if getattr(original, "winerror", None) != 5:
            raise
        try:
            if _later_local_creation(identity):
                return True
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            subprocess.SubprocessError,
        ):
            pass
        raise
