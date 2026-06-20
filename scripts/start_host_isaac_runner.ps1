param(
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1",
    [string]$WorkingDirectory = "C:\Dev\IsaacSim",
    [string]$PythonBat = "C:\Dev\IsaacSim\_build\windows-x86_64\release\python.bat",
    [string]$EntryScript = "C:\Dev\IsaacSim\_build\windows-x86_64\release\standalone_examples\api\isaacsim.robot.manipulators\ur5\pick_up_example.py",
    [string]$SeedDbPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$env:ISAAC_HOST_RUNNER_HOST = $HostAddress
$env:ISAAC_HOST_RUNNER_PORT = [string]$Port
$env:ISAAC_WORKING_DIRECTORY = $WorkingDirectory
$env:ISAAC_PYTHON_BAT = $PythonBat
$env:ISAAC_UR5_ENTRY_SCRIPT = $EntryScript

if ($SeedDbPath -ne "") {
    $env:ISAAC_SEED_DB_PATH = $SeedDbPath
}

Write-Host "Starting Isaac host runner..."
Write-Host "Project root: $ProjectRoot"
Write-Host "URL: http://$HostAddress`:$Port"
Write-Host "Working directory: $WorkingDirectory"
Write-Host "python.bat: $PythonBat"
Write-Host "Entry script: $EntryScript"

python .\host_isaac_runner_service.py
