param([string]$SessionFile = ".\workbench-output\companion-session.json")

$ErrorActionPreference = "Stop"
$sessionPath = [System.IO.Path]::GetFullPath($SessionFile)
if (-not (Test-Path -LiteralPath $sessionPath -PathType Leaf)) {
    Write-Output "Companion is already stopped."
    exit 0
}
$session = Get-Content -LiteralPath $sessionPath -Raw | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$session.pid)"
if (-not $process -or $process.CommandLine -notmatch "sovereign_workbench\.companion") {
    throw "Refusing to stop PID $($session.pid): it is not the recorded companion process."
}
Stop-Process -Id ([int]$session.pid)
Start-Sleep -Milliseconds 300
if (Test-Path -LiteralPath $sessionPath) { Remove-Item -LiteralPath $sessionPath }
Write-Output "Companion stopped."
