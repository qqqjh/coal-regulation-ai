param(
    [switch]$SkipFrontend,
    [string]$SshHost = "connect.westc.seetacloud.com",
    [int]$SshPort = 32390
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectDir ".runtime"
$KeyPath = Join-Path $HOME ".ssh\coal_regulation_server_ed25519"
$TunnelPidFile = Join-Path $RuntimeDir "ssh-tunnel.pid"
$FrontendPidFile = Join-Path $RuntimeDir "frontend.pid"
$FrontendPort = 3000

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
if (-not $env:COAL_SSH_PASSWORD -and -not (Test-Path -LiteralPath $KeyPath)) {
    throw "SSH key not found: $KeyPath"
}

$backendReady = $false
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
    $backendReady = $health.status -eq "healthy"
} catch {}

if (-not $backendReady) {
    if ($env:COAL_SSH_PASSWORD) {
        $forwardScript = Join-Path $PSScriptRoot "paramiko_forward.py"
        $tunnel = Start-Process -FilePath "python.exe" -ArgumentList @(
            $forwardScript, $SshHost, "$SshPort", "root",
            "8000", "127.0.0.1", "8000"
        ) -WindowStyle Hidden -PassThru
    } else {
        $sshArgs = @(
            "-i", $KeyPath,
            "-p", "$SshPort",
            "-N",
            "-L", "8000:127.0.0.1:8000",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "root@$SshHost"
        )
        $tunnel = Start-Process -FilePath "ssh.exe" -ArgumentList $sshArgs -WindowStyle Hidden -PassThru
    }
    Set-Content -LiteralPath $TunnelPidFile -Value $tunnel.Id

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
            if ($health.status -eq "healthy") {
                $backendReady = $true
                break
            }
        } catch {}
    }
}

if (-not $backendReady) {
    throw "The SSH tunnel started, but the remote backend health check failed."
}

Write-Host "Remote backend is available at http://127.0.0.1:8000"
if ($SkipFrontend) {
    exit 0
}

$frontendReady = $false
try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort" -TimeoutSec 2
    $frontendReady = $response.StatusCode -eq 200
} catch {}

if ($frontendReady) {
    Write-Host "Frontend is already available at http://127.0.0.1:$FrontendPort"
    exit 0
}

$frontend = Start-Process -FilePath "npm.cmd" -ArgumentList @(
    "run", "dev", "--", "--host", "127.0.0.1", "--port", "$FrontendPort", "--strictPort"
) -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru
Set-Content -LiteralPath $FrontendPidFile -Value $frontend.Id

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort" -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $frontendReady = $true
            break
        }
    } catch {}
}

if (-not $frontendReady) {
    throw "Frontend process started, but http://127.0.0.1:$FrontendPort did not become ready."
}

Write-Host "Frontend is available at http://127.0.0.1:$FrontendPort (PID $($frontend.Id))"
