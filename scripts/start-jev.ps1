# Starts the Open-Jev decision server used by the coordinator (Docker Desktop with GPU support required).
#   .\scripts\start-jev.ps1          2B checkpoint (~6 GB VRAM)
#   .\scripts\start-jev.ps1 -Big     9B checkpoint (~20 GB VRAM)
param([switch]$Big)
$root = Split-Path -Parent $PSScriptRoot
$env:JEV_CHECKPOINT = if ($Big) { "9b" } else { "2b" }
docker compose -f "$root\jev-router\compose.yml" up -d --build
Write-Host "Open-Jev $($env:JEV_CHECKPOINT) starting on http://127.0.0.1:8791 (first start downloads weights unless m3 already did)."
