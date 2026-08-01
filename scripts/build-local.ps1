param(
    [string]$Output = "paper/main.pdf"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    typst compile paper/main.typ $Output --root .
    Write-Host "Built $Output"
}
finally {
    Pop-Location
}
