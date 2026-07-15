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
$Script = Join-Path $PSScriptRoot "scripts\evaluate_manual_error_injection_v1.py"
$ReportDir = Join-Path $PSScriptRoot "reports\s1302_manual_error_eval_v10"

& $Python $Script `
    --manage-services `
    --service-python $Python `
    --worker-script v10_worker.py `
    --report-dir $ReportDir `
    @ExtraArgs
exit $LASTEXITCODE
