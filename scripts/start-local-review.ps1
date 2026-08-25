param(
    [Parameter(Mandatory = $true)][string]$StateDb,
    [string]$Reviewer = "Dominic Annand"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Workbench virtual environment not found. Create .venv and install the project first."
}

$statePath = [System.IO.Path]::GetFullPath($StateDb)
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw "The review database does not exist. Create a bounded archive batch first."
}
if ([string]::IsNullOrWhiteSpace($Reviewer)) {
    throw "A human reviewer name is required."
}

& $python -m sovereign_workbench.local_review --state-db $statePath --reviewer $Reviewer
