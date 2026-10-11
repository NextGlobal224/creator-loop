param([string]$DataRoot=(Join-Path $env:LOCALAPPDATA 'CreatorLoopBeta\data'))
$ErrorActionPreference='Stop'
$env:CREATOR_LOOP_DATA_ROOT=$DataRoot
$exe=Join-Path $PSScriptRoot 'CreatorLoop.exe'
if(-not (Test-Path -LiteralPath (Join-Path $DataRoot 'manifests\facebook-browser-runtime.json'))){
 $install=Start-Process -FilePath $exe -ArgumentList '--install-facebook-browser' -WindowStyle Hidden -PassThru
 $retainedInstallHandle=$install.Handle
 $install.WaitForExit()
 if($install.ExitCode -ne 0){throw 'Browser component installation failed; no user data was discarded.'}
}
$child=Start-Process -FilePath $exe -WindowStyle Hidden -PassThru
$retainedChildHandle=$child.Handle
$child.WaitForExit()
exit $child.ExitCode
