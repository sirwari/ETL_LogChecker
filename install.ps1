param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $RootDir ".venv"

$PythonExe = $null
$PythonArgs = @()

if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"
    $PythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = "python"
} else {
    throw "Python 3.11+ is required but was not found on PATH."
}

& $PythonExe @PythonArgs -m venv $VenvDir

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Virtualenv python not found at $VenvPython"
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $RootDir "requirements.txt")

if ($Dev) {
    & $VenvPython -m pip install -r (Join-Path $RootDir "requirements-dev.txt")
}

Write-Host "ETL_LogChecker is installed in $VenvDir"
Write-Host "Start the app with: .\start.ps1"
