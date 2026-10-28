param([string]$SessionFile = ".\workbench-output\companion-session.json")

$ErrorActionPreference = "Stop"
$sessionPath = [System.IO.Path]::GetFullPath($SessionFile)
if (-not (Test-Path -LiteralPath $sessionPath -PathType Leaf)) {
    Write-Output "Companion is already stopped."
    exit 0
}
# Read the exact bytes once: the same bytes identify the file again after the stop.
$before = [System.IO.File]::ReadAllBytes($sessionPath)
$sha = [System.Security.Cryptography.SHA256]::Create()
$beforeHash = [System.BitConverter]::ToString($sha.ComputeHash($before))
$session = [System.Text.Encoding]::UTF8.GetString($before) | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$session.pid)"
if (-not $process -or $process.CommandLine -notmatch "sovereign_workbench\.companion") {
    throw "Refusing to stop PID $($session.pid): it is not the recorded companion process."
}
Stop-Process -Id ([int]$session.pid)
Start-Sleep -Milliseconds 300
# Remove the session file only if it is still the file read above: a regular file
# (not a link) with unchanged bytes. Anything else is left in place and reported.
if (Test-Path -LiteralPath $sessionPath) {
    $item = Get-Item -LiteralPath $sessionPath -Force
    $unchanged = $false
    if (-not $item.PSIsContainer -and -not ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        $afterHash = [System.BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($sessionPath)))
        $unchanged = ($afterHash -eq $beforeHash)
    }
    if ($unchanged) { Remove-Item -LiteralPath $sessionPath }
    else { Write-Output "Session file changed while stopping; it was left in place: $sessionPath" }
}
Write-Output "Companion stopped."
