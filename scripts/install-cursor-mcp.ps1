# Adds (or repairs) the Blackwell MCP server in Cursor's global config: %USERPROFILE%\.cursor\mcp.json
# Other MCP servers in the file are kept; the old file is backed up next to it.
#   .\scripts\install-cursor-mcp.ps1                          # coordinator on this PC
#   .\scripts\install-cursor-mcp.ps1 -Url http://192.168.88.3:9477   # coordinator on another PC
param([string]$Url = "http://127.0.0.1:9477")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Push-Location $root; uv sync; Pop-Location }
$dir = Join-Path $env:USERPROFILE ".cursor"
$cfg = Join-Path $dir "mcp.json"
New-Item -ItemType Directory -Force $dir | Out-Null
$data = @{ mcpServers = @{} }
if (Test-Path $cfg) {
  Copy-Item $cfg "$cfg.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
  $raw = Get-Content $cfg -Raw
  if ($raw.Trim()) { $data = $raw | ConvertFrom-Json }
  if (-not $data.mcpServers) { $data | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) }
}
# python.exe -m ... rather than the generated blackwell-mcp.exe: Windows Application Control blocks that launcher here.
$entry = [pscustomobject]@{
  command = $py
  args    = @("-m", "blackwell_mcp.server")
  env     = [pscustomobject]@{ BLACKWELL_COORDINATOR_URL = $Url; BLACKWELL_OLLAMA_MODEL = "qwen2.5:7b"; PYTHONIOENCODING = "utf-8" }
}
if ($data.mcpServers.PSObject.Properties.Name -contains "blackwell-coordinator") { $data.mcpServers."blackwell-coordinator" = $entry }
else { $data.mcpServers | Add-Member -NotePropertyName "blackwell-coordinator" -NotePropertyValue $entry }
$json = $data | ConvertTo-Json -Depth 10
[IO.File]::WriteAllText($cfg, $json, (New-Object Text.UTF8Encoding $false))
Write-Host "Blackwell MCP added to $cfg (coordinator: $Url)."
Write-Host "In Cursor: Settings > MCP > make sure 'blackwell-coordinator' is enabled (green dot), then type /activate in chat."
