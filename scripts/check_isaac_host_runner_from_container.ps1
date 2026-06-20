param(
    [string]$HostRunnerHealthUrl = "http://host.docker.internal:8765/health",
    [string]$BackendStatusUrl = "http://localhost:8000/debug/isaac-host-runner-status"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

Write-Host "== docker compose config: ISAAC_HOST_RUNNER_URL =="
$composeConfig = docker compose config
$composeConfig | Select-String -Pattern "ISAAC_HOST_RUNNER_URL|HOST_PROJECT_ROOT|ISAAC_EXECUTION_MODE|CONTAINER_PROJECT_ROOT"

Write-Host ""
Write-Host "== trt-api container -> host runner health =="
docker compose exec trt-api python -c "import urllib.request; print(urllib.request.urlopen('$HostRunnerHealthUrl', timeout=3).read().decode())"

Write-Host ""
Write-Host "== backend host-runner status =="
Invoke-RestMethod $BackendStatusUrl | ConvertTo-Json -Depth 8
