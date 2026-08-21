param(
    [string]$StateDb = ".\workbench-output\state.db",
    [string]$StagingDb = ".\workbench-output\staging.db",
    [string]$AllowOrigin = "http://localhost:5173",
    [ValidateRange(1024, 65535)][int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$starter = Join-Path $PSScriptRoot "start-companion.ps1"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Sovereign Workbench Companion.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$starter`" -StateDb `"$StateDb`" -StagingDb `"$StagingDb`" -AllowOrigin `"$AllowOrigin`" -Port $Port"
$shortcut.WorkingDirectory = $repoRoot
$shortcut.Description = "Start the localhost-only Sovereign Workbench companion"
$shortcut.Save()
Write-Output "Created $shortcutPath"
