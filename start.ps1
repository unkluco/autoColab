param([switch]$Background, [switch]$Once, [switch]$DryRun, [switch]$Check, [string]$ConfigFile = '')
$ErrorActionPreference = 'Stop'
$taskPython = (Get-Command python -ErrorAction Stop).Source
$taskMain = Join-Path $PSScriptRoot 'main.py'
$taskConfig = if ($ConfigFile) { (Resolve-Path -LiteralPath $ConfigFile).Path } else { Join-Path $PSScriptRoot 'config.toml' }
if ($Background -and ($Once -or $DryRun -or $Check)) { throw 'Background cannot be combined with Once/DryRun/Check.' }
if (-not $Background) {
    $taskArguments = @($taskMain, '--config', $taskConfig)
    if ($Once) { $taskArguments += '--once' }
    if ($DryRun) { $taskArguments += '--dry-run' }
    if ($Check) { $taskArguments += '--check' }
    & $taskPython @taskArguments
    exit $LASTEXITCODE
}
& $taskPython $taskMain --config $taskConfig --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$taskRuntime = (& $taskPython $taskMain --config $taskConfig --runtime-dir).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not read runtime directory.' }
$taskExisting = (& $taskPython $taskMain --config $taskConfig --status | ConvertFrom-Json)
if ($taskExisting.running) { Write-Output 'Worker is already running.'; exit 0 }
New-Item -ItemType Directory -Force -Path $taskRuntime | Out-Null
$taskArgumentLine = '"' + $taskMain + '" --config "' + $taskConfig + '" --quiet'
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList $taskArgumentLine -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRuntime 'startup-out.log') -RedirectStandardError (Join-Path $taskRuntime 'startup-error.log')
for ($taskAttempt = 0; $taskAttempt -lt 20; $taskAttempt++) {
    Start-Sleep -Milliseconds 250
    $taskProcess.Refresh()
    if ($taskProcess.HasExited) {
        Get-Content -LiteralPath (Join-Path $taskRuntime 'startup-error.log')
        throw 'Worker exited during startup; check runtime logs.'
    }
    $taskStatus = (& $taskPython $taskMain --config $taskConfig --status | ConvertFrom-Json)
    if ($taskStatus.running -and $taskStatus.pid) { Write-Output ('Worker started in background. PID: ' + $taskStatus.pid); exit 0 }
}
throw 'Startup not confirmed; use status.ps1 and inspect runtime logs.'
