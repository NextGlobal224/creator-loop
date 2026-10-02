param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [string[]]$CommandArgs = @(),
    [ValidateRange(1, 86400)][int]$TimeoutSeconds = 120,
    [string]$LogRoot = '.local-test-logs'
)
$ErrorActionPreference = 'Stop'

# The gated shell cannot launch the test until it belongs to our Job Object.
# Closing that job releases only this invocation's process tree, including Qt
# children and the venv Python launcher. No PID/name based global termination.
if (-not ('CreatorLoopTestJob' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public sealed class CreatorLoopTestJob : IDisposable {
    private IntPtr handle;
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr handle);
    public CreatorLoopTestJob() {
        handle=CreateJobObject(IntPtr.Zero,null);
        if(handle==IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
    }
    public void Assign(IntPtr process) {
        if(!AssignProcessToJobObject(handle,process))
            throw new System.ComponentModel.Win32Exception();
    }
    public void Dispose() {
        if(handle!=IntPtr.Zero) {
            // Explicit termination also cleans up children after normal test exit.
            TerminateJobObject(handle,124);
            CloseHandle(handle); handle=IntPtr.Zero;
        }
    }
}
'@
}

function Quote-Literal([string]$Value) { "'" + $Value.Replace("'", "''") + "'" }
$resolvedExecutable = (Get-Command $Executable -ErrorAction Stop).Source
if (-not $resolvedExecutable) { throw 'Executable must resolve to an application.' }
$runFolder = Join-Path ([IO.Path]::GetFullPath($LogRoot)) (
    (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')
)
[IO.Directory]::CreateDirectory($runFolder) | Out-Null
$logPath = Join-Path $runFolder 'output.log'
$gateName = 'Local\CreatorLoopTest-' + [guid]::NewGuid().ToString('N')
$gate = [Threading.EventWaitHandle]::new($false, [Threading.EventResetMode]::ManualReset, $gateName)
$job = [CreatorLoopTestJob]::new()
$process = $null
$exitCode = 125
$timedOut = $false
$failure = $null
$watch = [Diagnostics.Stopwatch]::StartNew()
$argumentsLiteral = '@(' + (($CommandArgs | ForEach-Object { Quote-Literal $_ }) -join ',') + ')'
$childScript = @"
`$ErrorActionPreference = 'Stop'
`$gate = [Threading.EventWaitHandle]::OpenExisting($(Quote-Literal $gateName))
if (-not `$gate.WaitOne(30000)) { exit 125 }
`$gate.Dispose()
Set-Location -LiteralPath $(Quote-Literal (Get-Location).Path)
`$global:LASTEXITCODE = 0
try {
    `$ErrorActionPreference = 'Continue'
    & $(Quote-Literal $resolvedExecutable) $argumentsLiteral *> $(Quote-Literal $logPath)
    exit `$LASTEXITCODE
} catch {
    `$_ | Out-File -LiteralPath $(Quote-Literal $logPath) -Append -Encoding utf8
    exit 125
}
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childScript))
try {
    $process = Start-Process -FilePath "$PSHOME\powershell.exe" -ArgumentList @(
        '-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded
    ) -WindowStyle Hidden -PassThru
    $job.Assign($process.Handle)
    $gate.Set() | Out-Null
    if ($process.WaitForExit($TimeoutSeconds * 1000)) {
        $process.Refresh()
        $exitCode = $process.ExitCode
    } else {
        $timedOut = $true
        $exitCode = 124
    }
} catch {
    $failure = $_.Exception.Message
    # If assignment failed, our gated shell has not launched a test.
    if ($process -and -not $process.HasExited) { $process.Kill() }
} finally {
    $job.Dispose()
    if ($process) { $process.WaitForExit(5000) | Out-Null }
    $gate.Dispose()
    $watch.Stop()
    [ordered]@{
        executable = $resolvedExecutable
        arguments = $CommandArgs
        timeout_seconds = $TimeoutSeconds
        timed_out = $timedOut
        exit_code = $exitCode
        elapsed_seconds = $watch.Elapsed.TotalSeconds
        wrapper_pid = $(if ($process) { $process.Id } else { $null })
        failure = $failure
        output_log = $logPath
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runFolder 'result.json') -Encoding UTF8
    if ($process) { $process.Dispose() }
}
Write-Output "Test exit=$exitCode timeout=$timedOut log=$runFolder"
exit $exitCode
