param(
    [Parameter(Mandatory = $true)][string]$StateDb,
    [Parameter(Mandatory = $true)][string]$StagingDb,
    [string[]]$AllowOrigin = @("http://localhost:5173"),
    [ValidateRange(1024, 65535)][int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Workbench virtual environment not found. Create .venv and install the project first."
}

$statePath = [System.IO.Path]::GetFullPath($StateDb)
$stagingPath = [System.IO.Path]::GetFullPath($StagingDb)
New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($statePath)) | Out-Null
New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($stagingPath)) | Out-Null

$arguments = @("-m", "sovereign_workbench.companion", "--state-db", $statePath,
    "--staging-db", $stagingPath, "--port", $Port)
foreach ($origin in $AllowOrigin) {
    $arguments += @("--allow-origin", $origin)
}
& $python @arguments
