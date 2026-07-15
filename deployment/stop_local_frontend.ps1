$ProjectDir = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectDir ".runtime"

function Get-DescendantProcessIds([int]$ParentId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        Get-DescendantProcessIds -ParentId $child.ProcessId
        $child.ProcessId
    }
}

foreach ($name in @("frontend", "ssh-tunnel")) {
    $pidFile = Join-Path $RuntimeDir "$name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }
    $processId = [int](Get-Content -LiteralPath $pidFile)
    $descendants = @(Get-DescendantProcessIds -ParentId $processId)
    foreach ($descendantId in ($descendants | Sort-Object -Descending)) {
        Stop-Process -Id $descendantId -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $processId -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile -Force
    Write-Host "Stopped $name (PID $processId)"
}
