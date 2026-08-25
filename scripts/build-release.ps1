param(
    [Parameter(Mandatory = $true)][ValidatePattern('^v\d+\.\d+\.\d+$')][string]$Version,
    [string]$OutputDirectory = ".\release-output"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null
$archive = Join-Path $output "sovereign-knowledge-workbench-$Version.zip"
if (Test-Path -LiteralPath $archive) { throw "Release archive already exists: $archive" }
git -C $repoRoot archive --format=zip --output=$archive HEAD
$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
$checksum = Join-Path $output "SHA256SUMS-$Version.txt"
if (Test-Path -LiteralPath $checksum) { throw "Checksum file already exists: $checksum" }
Set-Content -LiteralPath $checksum -Value "$hash  $([System.IO.Path]::GetFileName($archive))" -Encoding utf8NoBOM
Write-Output "Created unsigned release candidate $archive"
Write-Output "Created checksum evidence $checksum"
Write-Output "Signing remains a mandatory human-controlled release gate."
