# Installs the Blackwell Coordinator desktop app for the current user:
#   - Start menu entry "Blackwell Coordinator"
#   - starts hidden in the system tray at every login
# and opens it now. Remove: delete the two "Blackwell Coordinator" shortcuts under
# %APPDATA%\Microsoft\Windows\Start Menu\Programs (and its Startup folder).
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Push-Location $root; uv sync; Pop-Location }
& $py -m blackwell_app.app --install-shortcuts
Start-Process (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Blackwell Coordinator.lnk")
Write-Host "Blackwell Coordinator is open. Closing the window keeps it in the system tray (click ^ next to the clock)."
