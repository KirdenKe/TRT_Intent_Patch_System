param(
    [int]$Port = 8765,
    [string]$HostAddress = "0.0.0.0",
    [string]$WorkingDirectory = "",
    [string]$PythonBat = "",
    [string]$EntryScript = "",
    [string]$SeedDbPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

# HANDOVER CONFIGURATION: customize Isaac/project paths in
# data/isaac_host_config.json. Explicit command-line parameters override that
# file; the C:\Dev\IsaacSim values below are last-resort fallbacks only.
$ConfigPath = Join-Path $ProjectRoot "data\isaac_host_config.json"
$Config = $null
if (Test-Path -LiteralPath $ConfigPath) {
    $Config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
} else {
    throw "Isaac host configuration is missing: $ConfigPath. Create it for this machine before starting the runner."
}

if (-not $WorkingDirectory) {
    $WorkingDirectory = if ($Config.isaac_working_directory) { $Config.isaac_working_directory } else { "C:\Dev\IsaacSim" }
}
if (-not $PythonBat) {
    $PythonBat = if ($Config.python_bat) { $Config.python_bat } else { Join-Path $WorkingDirectory "_build\windows-x86_64\release\python.bat" }
}
if (-not $EntryScript) {
    $EntryScript = if ($Config.entry_script) {
        $Config.entry_script
    } else {
        Join-Path $WorkingDirectory "_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\pick_up_example.py"
    }
}
if (-not $SeedDbPath -and $Config.seed_db_path) {
    $SeedDbPath = $Config.seed_db_path
}

$PathErrors = @()
if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
    $PathErrors += "Isaac working directory does not exist: $WorkingDirectory"
}
if (-not (Test-Path -LiteralPath $PythonBat -PathType Leaf)) {
    $PathErrors += "Isaac python.bat does not exist: $PythonBat"
}
if (-not (Test-Path -LiteralPath $EntryScript -PathType Leaf)) {
    $PathErrors += "Isaac entry script does not exist: $EntryScript"
}

$ConfiguredProjectRoot = [string]$Config.host_project_root
if (-not $ConfiguredProjectRoot) {
    $PathErrors += "host_project_root is missing from $ConfigPath"
} elseif (-not (Test-Path -LiteralPath $ConfiguredProjectRoot -PathType Container)) {
    $PathErrors += "Configured host_project_root does not exist: $ConfiguredProjectRoot"
} else {
    $ResolvedConfiguredRoot = (Resolve-Path -LiteralPath $ConfiguredProjectRoot).Path.TrimEnd('\')
    $ResolvedProjectRoot = $ProjectRoot.Path.TrimEnd('\')
    if (-not $ResolvedConfiguredRoot.Equals($ResolvedProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        $PathErrors += "Configured host_project_root points to '$ResolvedConfiguredRoot', but this clone is '$ResolvedProjectRoot'."
    }
}

if ($PathErrors.Count -gt 0) {
    Write-Host "Isaac host-runner preflight failed." -ForegroundColor Red
    foreach ($PathError in $PathErrors) {
        Write-Host "- $PathError" -ForegroundColor Red
    }
    Write-Host "Update data\isaac_host_config.json for this machine, then run this script again." -ForegroundColor Yellow
    exit 1
}

if ($SeedDbPath -and -not (Test-Path -LiteralPath $SeedDbPath -PathType Leaf)) {
    Write-Warning "Seed database does not exist: $SeedDbPath. The runner will start, but this path will not be passed to Isaac Sim."
}

$env:ISAAC_HOST_RUNNER_HOST = $HostAddress
$env:ISAAC_HOST_RUNNER_PORT = [string]$Port
$env:ISAAC_WORKING_DIRECTORY = $WorkingDirectory
$env:ISAAC_PYTHON_BAT = $PythonBat
$env:ISAAC_UR5_ENTRY_SCRIPT = $EntryScript

if ($SeedDbPath -ne "") {
    $env:ISAAC_SEED_DB_PATH = $SeedDbPath
} else {
    Remove-Item Env:ISAAC_SEED_DB_PATH -ErrorAction SilentlyContinue
}

Write-Host "Starting Isaac host runner..."
Write-Host "Project root: $ProjectRoot"
Write-Host "Host configuration: $ConfigPath"
Write-Host "Bind address: http://$HostAddress`:$Port"
Write-Host "Windows health check: http://127.0.0.1`:$Port/health"
Write-Host "Docker target: http://host.docker.internal`:$Port"
Write-Host "Working directory: $WorkingDirectory"
Write-Host "python.bat: $PythonBat"
Write-Host "Entry script: $EntryScript"
Write-Host "Seed database: $SeedDbPath"
Write-Host "The HTTP runner is starting now. Isaac Sim starts only after an accepted POST to /isaac/run or /isaac/runs."

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ServicePython = if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    $VenvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}
Write-Host "Host service Python: $ServicePython"
& $ServicePython .\host_isaac_runner_service.py
exit $LASTEXITCODE
