param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $RootDir ".venv\Scripts\python.exe"

if (Test-Path $VenvPython) {
    $PythonExe = $VenvPython
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"
    $Arguments = @("-3", (Join-Path $RootDir "etl_logchecker.py")) + $Arguments
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = "python"
} else {
    throw "Python 3.11+ is required but was not found on PATH."
}

if (-not ($Arguments)) {
    if ($PythonExe -eq "py") {
        $Arguments = @("-3", (Join-Path $RootDir "etl_logchecker.py"), "--gui")
    } else {
        $Arguments = @((Join-Path $RootDir "etl_logchecker.py"), "--gui")
    }
} elseif ($PythonExe -ne "py") {
    $Arguments = @((Join-Path $RootDir "etl_logchecker.py")) + $Arguments
}

& $PythonExe @Arguments
exit $LASTEXITCODE
