param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PreferredPython = "D:\Anaconda\envs\langchain0.3\python.exe"
$Python = if (Test-Path -LiteralPath $PreferredPython) {
    $PreferredPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$Script = Join-Path $PSScriptRoot "scripts\evaluate_compliance_trace_v10.py"

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

& $Python $Script @ExtraArgs
exit $LASTEXITCODE
