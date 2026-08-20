param(
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1",
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
Write-Host "URL: http://$HostAddress`:$Port"
Write-Host "Working directory: $WorkingDirectory"
Write-Host "python.bat: $PythonBat"
Write-Host "Entry script: $EntryScript"
Write-Host "Seed database: $SeedDbPath"

python .\host_isaac_runner_service.py
