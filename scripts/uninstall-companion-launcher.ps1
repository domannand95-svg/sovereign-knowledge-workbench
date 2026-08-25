$ErrorActionPreference = "Stop"
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Sovereign Workbench Companion.lnk"
if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    Remove-Item -LiteralPath $shortcutPath
    Write-Output "Removed $shortcutPath. Repository files and Workbench data were not removed."
} else {
    Write-Output "Launcher is not installed. Nothing was removed."
}
