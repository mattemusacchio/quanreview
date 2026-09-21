# Multi-Annotator Launch Script
# Usage: .\run_annotator.ps1 <annotator_id>
# Example: .\run_annotator.ps1 annotator_a

param(
    [Parameter(Mandatory=$true, Position=0, HelpMessage="Annotator ID (e.g., annotator_a, annotator_b, annotator_c)")]
    [string]$Annotator,

    [Parameter(Mandatory=$false, HelpMessage="Path to annotation project directory")]
    [string]$ProjectDir = "data/annotation_project"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$backendDir = Join-Path $repoRoot "web\backend"
$schemaPath = Join-Path $repoRoot "comparison_schema.yaml"
$groundTruthDir = Join-Path $repoRoot "data\sample\ground_truth"
$modelTagsDir = Join-Path $repoRoot "data\sample\model_tags"

if ([System.IO.Path]::IsPathRooted($ProjectDir)) {
    $resolvedProjectDir = [System.IO.Path]::GetFullPath($ProjectDir)
} else {
    $resolvedProjectDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $ProjectDir))
}

# Verify assignment file exists
$assignmentFile = Join-Path $resolvedProjectDir "assignments/$Annotator.json"
if (-not (Test-Path $assignmentFile)) {
    Write-Host "ERROR: No assignment found for annotator '$Annotator'." -ForegroundColor Red
    Write-Host "  Expected: $assignmentFile" -ForegroundColor Yellow
    Write-Host "  Run 'uv run python scripts/orchestrate.py' first." -ForegroundColor Yellow
    exit 1
}

$docCount = (Get-Content $assignmentFile | ConvertFrom-Json).Count
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  NER Tag Validator - Annotator Mode" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Annotator:  $Annotator" -ForegroundColor White
Write-Host "  Documents:  $docCount assigned" -ForegroundColor White
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$outputFile = Join-Path $resolvedProjectDir "results\$Annotator\output.json"
$correctedDir = Join-Path $resolvedProjectDir "results\$Annotator"
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (Test-Path $pythonExe) {
    Push-Location $backendDir
    try {
        & $pythonExe ".\web_app.py" `
            --ground-truth-dir $groundTruthDir `
            --model-tags-dir $modelTagsDir `
            --output-file $outputFile `
            --corrected-dir $correctedDir `
            --schema $schemaPath `
            --annotator $Annotator `
            --project-dir $resolvedProjectDir
    } finally {
        Pop-Location
    }
} else {
    Push-Location $repoRoot
    try {
        uv run python ".\web\backend\web_app.py" `
            --ground-truth-dir $groundTruthDir `
            --model-tags-dir $modelTagsDir `
            --output-file $outputFile `
            --corrected-dir $correctedDir `
            --schema $schemaPath `
            --annotator $Annotator `
            --project-dir $resolvedProjectDir
    } finally {
        Pop-Location
    }
}
