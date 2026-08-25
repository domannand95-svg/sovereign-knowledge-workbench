param([string]$SessionFile = ".\workbench-output\companion-session.json")

$ErrorActionPreference = "Stop"
$sessionPath = [System.IO.Path]::GetFullPath($SessionFile)
if (-not (Test-Path -LiteralPath $sessionPath -PathType Leaf)) {
    Write-Output "Companion is not running (no session file)."
    exit 1
}
$session = Get-Content -LiteralPath $sessionPath -Raw | ConvertFrom-Json
$headers = @{
    Authorization = "Bearer $($session.token)"
    "X-Companion-Contract" = $session.contract_version
}
try {
    $health = Invoke-RestMethod -Uri "$($session.url)/v1/health" -Headers $headers -TimeoutSec 3
    Write-Output "Companion healthy at $($session.url); authority=$($health.authority); pid=$($session.pid)"
} catch {
    Write-Output "Companion session exists but health check failed: $($_.Exception.Message)"
    exit 2
}
