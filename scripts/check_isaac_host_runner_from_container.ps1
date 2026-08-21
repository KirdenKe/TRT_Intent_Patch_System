param(
    [string]$LocalHostRunnerHealthUrl = "http://127.0.0.1:8765/health",
    [string]$LocalHostRunnerRunsUrl = "http://127.0.0.1:8765/isaac/runs",
    [string]$HostRunnerHealthUrl = "http://host.docker.internal:8765/health",
    [string]$BackendHealthUrl = "http://localhost:8000/health",
    [string]$BackendStatusUrl = "http://localhost:8000/debug/isaac-host-runner-status"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

function Write-HttpFailureDetail {
    param([System.Management.Automation.ErrorRecord]$ErrorRecord)

    Write-Host "Error: $($ErrorRecord.Exception.Message)" -ForegroundColor Red
    if ($ErrorRecord.ErrorDetails -and $ErrorRecord.ErrorDetails.Message) {
        Write-Host "Response body: $($ErrorRecord.ErrorDetails.Message)" -ForegroundColor Red
    }
    if ($ErrorRecord.Exception.Response -and $ErrorRecord.Exception.Response.StatusCode) {
        Write-Host "HTTP status: $($ErrorRecord.Exception.Response.StatusCode)" -ForegroundColor Red
    }
}

Write-Host "== Windows -> host runner health =="
try {
    $LocalHealth = Invoke-RestMethod -Uri $LocalHostRunnerHealthUrl -TimeoutSec 3
    $LocalHealth | ConvertTo-Json -Depth 8
} catch {
    Write-Host "FAILED: Windows cannot reach $LocalHostRunnerHealthUrl" -ForegroundColor Red
    Write-Host "Start the service in another PowerShell window:" -ForegroundColor Yellow
    Write-Host "  .\scripts\start_host_isaac_runner.ps1 -HostAddress 0.0.0.0 -Port 8765"
    Write-Host "The runner must remain open while Docker and Isaac tests are running."
    exit 1
}

if ($LocalHealth.status -ne "OK" -or -not $LocalHealth.ready) {
    Write-Host "FAILED: The host runner is reachable but its Isaac paths are not ready." -ForegroundColor Red
    Write-Host "Update data\isaac_host_config.json until python_bat_exists, entry_script_exists, and working_directory_exists are all true." -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "== recent host-runner requests =="
try {
    $RecentRuns = Invoke-RestMethod -Uri $LocalHostRunnerRunsUrl -TimeoutSec 3
    if ($RecentRuns.run_count -eq 0) {
        Write-Host "No run requests have been recorded since this host-runner process started."
    } else {
        $RecentRuns.runs |
            Select-Object run_id, status, launch_attempted, process_started, pid, return_code, errors, missing_paths |
            Format-List
    }
} catch {
    Write-Warning "Recent-run diagnostics are unavailable. Restart the host runner after updating the project if this endpoint returns 405."
    Write-HttpFailureDetail $_
}

Write-Host ""
Write-Host "== docker compose config: ISAAC_HOST_RUNNER_URL =="
$composeConfig = docker compose config 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED: docker compose config could not be resolved." -ForegroundColor Red
    $composeConfig | ForEach-Object { Write-Host $_ }
    exit 1
}
$composeConfig | Select-String -Pattern "ISAAC_HOST_RUNNER_URL|ISAAC_EXECUTION_MODE|CONTAINER_PROJECT_ROOT"

Write-Host ""
Write-Host "== trt-api container -> host runner health =="
$ContainerProbe = docker compose exec -T trt-api python -c "import urllib.request; print(urllib.request.urlopen('$HostRunnerHealthUrl', timeout=3).read().decode())" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED: trt-api cannot reach $HostRunnerHealthUrl" -ForegroundColor Red
    Write-Host "The Windows runner is healthy, so check these Docker boundary settings:" -ForegroundColor Yellow
    Write-Host "- The runner was started with -HostAddress 0.0.0.0."
    Write-Host "- Windows Firewall allows TCP port 8765 for the selected network profile."
    Write-Host "- .env contains ISAAC_HOST_RUNNER_URL=http://host.docker.internal:8765."
    Write-Host "- trt-api was recreated after .env changed: docker compose up -d --force-recreate trt-api"
    Write-Host "Raw container probe output is available with:"
    Write-Host "  docker compose exec -T trt-api python -c `"import urllib.request; print(urllib.request.urlopen('$HostRunnerHealthUrl', timeout=3).read().decode())`""
    exit 1
}
$ContainerProbe | ForEach-Object { Write-Host $_ }

Write-Host ""
Write-Host "== Windows -> trt-api health =="
try {
    $BackendHealth = Invoke-RestMethod -Uri $BackendHealthUrl -TimeoutSec 5
    $BackendHealth | ConvertTo-Json -Depth 4
} catch {
    Write-Host "FAILED: Could not read $BackendHealthUrl" -ForegroundColor Red
    Write-HttpFailureDetail $_
    Write-Host "Inspect the active container:" -ForegroundColor Yellow
    Write-Host "  docker compose ps trt-api"
    Write-Host "  docker compose logs --tail=200 trt-api"
    Write-Host "Then rebuild and recreate it if necessary:"
    Write-Host "  docker compose up -d --build --force-recreate trt-api"
    exit 1
}

Write-Host ""
Write-Host "== backend host-runner status =="
try {
    $BackendStatus = Invoke-RestMethod -Uri $BackendStatusUrl -TimeoutSec 8
    $BackendStatus | ConvertTo-Json -Depth 8
} catch {
    Write-Host "FAILED: trt-api health passed, but $BackendStatusUrl failed." -ForegroundColor Red
    Write-HttpFailureDetail $_
    Write-Host "This usually means the running trt-api image is stale or the debug route raised an application error." -ForegroundColor Yellow
    Write-Host "Inspect and recreate it:"
    Write-Host "  docker compose logs --tail=200 trt-api"
    Write-Host "  docker compose up -d --build --force-recreate trt-api"
    exit 1
}

if (-not $BackendStatus.available) {
    Write-Host "FAILED: trt-api reports that the host runner is unavailable." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "PASS: Windows, Docker, and trt-api can all reach the configured Isaac host runner." -ForegroundColor Green
