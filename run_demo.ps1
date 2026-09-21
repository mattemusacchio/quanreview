# Launch the annotation UI on the bundled synthetic sample corpus.
#
# This is the entry point for anyone evaluating the tool: it needs no data of
# your own and no configuration. Results are written to data\sample_run\ and
# can be deleted freely.
#
#   .\run_demo.cmd

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SampleDir = Join-Path $ScriptDir "data\sample"
$RunDir    = Join-Path $ScriptDir "data\sample_run"

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @(
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$Uv = Find-Uv
if (-not $Uv) {
    Write-Host "ERROR: uv is required but was not found on PATH."
    Write-Host "  Install it from https://docs.astral.sh/uv/ and run this script again."
    exit 1
}

# The sample corpus is generated, not committed, so a fresh clone builds it on
# first run. Regenerating is cheap and deterministic.
if (-not (Test-Path (Join-Path $SampleDir "ground_truth"))) {
    Write-Host "Generating the sample corpus..."
    Push-Location $ScriptDir
    & $Uv run python ".\scripts\make_sample_data.py"
    $genExit = $LASTEXITCODE
    Pop-Location
    if ($genExit -ne 0) { exit $genExit }
}

if (-not (Test-Path $RunDir)) {
    New-Item -ItemType Directory -Path $RunDir | Out-Null
}

# Point the backend at the sample corpus's own source texts rather than the
# project-wide location, which a fresh clone does not have.
$env:NER_SOURCE_TEXT_DIR = Join-Path $SampleDir "source"

Write-Host "Starting the annotation UI on the sample corpus..."
Write-Host "  corpus:  $SampleDir"
Write-Host "  results: $RunDir"
Write-Host ""

# Stay at the repository root. web\backend has a pyproject.toml of its own,
# declared in poetry non-package mode, so running uv from inside it makes uv
# try to build that project and fail. Python still resolves the backend's local
# imports because it puts the script's own directory on sys.path.
Set-Location $ScriptDir
& $Uv run python ".\web\backend\web_app.py" `
    --ground-truth-dir (Join-Path $SampleDir "ground_truth") `
    --model-tags-dir (Join-Path $SampleDir "model_tags") `
    --output-file (Join-Path $RunDir "output.json") `
    --corrected-dir (Join-Path $RunDir "corrected") `
    --schema (Join-Path $ScriptDir "comparison_schema.yaml")
exit $LASTEXITCODE
