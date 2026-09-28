# Starts the BlackwellCoordinator daemon (dashboard + API on port 9477, reachable from the LAN).
#   .\scripts\start-coordinator.ps1              reads the real GPU via nvidia-smi
#   .\scripts\start-coordinator.ps1 -LocalOnly   bind 127.0.0.1 only
param([switch]$LocalOnly, [int]$Port = 9477)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path "$root\.venv")) { uv sync }
$argsList = @("-m", "coordinator.main", "--port", $Port)
if ($LocalOnly) { $argsList += "--local-only" }
& "$root\.venv\Scripts\python.exe" @argsList
