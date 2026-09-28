# Starts the coordinator now and at every logon.
# Registers a per-user scheduled task (also restarts it after a crash);
# if Task Scheduler refuses, falls back to a shortcut in your Startup folder (no crash restart).
# Remove: Unregister-ScheduledTask -TaskName BlackwellCoordinator -Confirm:$false
#         Remove-Item "$([Environment]::GetFolderPath('Startup'))\BlackwellCoordinator.lnk"
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# python.exe under a headless console: Windows Application Control blocks the venv's pythonw.exe on this machine.
$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Push-Location $root; uv sync; Pop-Location }
$exe = "$env:WINDIR\System32\conhost.exe"
$cmdArgs = "--headless `"$py`" -m coordinator.main --log-file"

$mode = $null
try {
  $action = New-ScheduledTaskAction -Execute $exe -Argument $cmdArgs -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName "BlackwellCoordinator" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Shared Blackwell GPU job coordinator (dashboard on :9477)" -Force | Out-Null
  $mode = "task"
} catch {
  $lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "BlackwellCoordinator.lnk"
  $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
  $sh.TargetPath = $exe
  $sh.Arguments = $cmdArgs
  $sh.WorkingDirectory = $root
  $sh.Description = "BlackwellCoordinator (dashboard on :9477)"
  $sh.Save()
  $mode = "startup"
}

if (-not (Get-NetTCPConnection -LocalPort 9477 -State Listen -ErrorAction SilentlyContinue)) {
  if ($mode -eq "task") { Start-ScheduledTask -TaskName "BlackwellCoordinator" }
  else { Start-Process -FilePath $exe -ArgumentList $cmdArgs -WorkingDirectory $root }
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
  $_.InterfaceAlias -notmatch "vEthernet|Loopback|WSL|VirtualBox|Docker" -and $_.IPAddress -notlike "169.254*" } |
  Select-Object -First 1).IPAddress
Write-Host ("Autostart: " + $(if ($mode -eq "task") { "scheduled task (restarts on crash)" } else { "Startup folder shortcut (run elevated for crash restarts)" }))
Write-Host "Dashboard: http://localhost:9477/   LAN: http://${ip}:9477/"
Write-Host "Log: $env:USERPROFILE\.blackwell\coordinator.log"
Write-Host "If teammates cannot reach it, allow the port (elevated):"
Write-Host "  New-NetFirewallRule -DisplayName BlackwellCoordinator -Direction Inbound -Protocol TCP -LocalPort 9477 -Action Allow -Profile Private"
