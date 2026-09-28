# Blackwell MCP Setup

How to connect Cursor to BlackwellCoordinator and run `/activate`.

## 1. Start the coordinator (GPU host)

```powershell
cd "C:\Users\Sirena Tech\Documents\BlackwellCoordinator"
uv sync
.\scripts\start-coordinator.ps1
```

Dashboard: http://localhost:9477/

## 2. MCP config (already added)

The `blackwell-coordinator` server is in your global Cursor config:

`C:\Users\Sirena Tech\.cursor\mcp.json`

Reload MCP in **Cursor Settings → MCP**, or restart Cursor. You should see one tool: **`activate`**, and the **`/activate`** slash prompt.

## 3. Use `/activate`

1. Open a training project on the GPU host in Cursor (local or Remote-SSH).
2. In chat, type **`/activate`** (or ask: *"Activate this project for Blackwell, owner alice, about 2 hours"*).
3. The agent calls `activate`, applies **Agent tasks** in the report, then calls `activate` again until status is **complete**.
4. Open the dashboard → **Start Now** or **Schedule**.

## Optional env vars

Add to the `env` block in `mcp.json` if needed:

| Variable | Purpose |
|----------|---------|
| `BLACKWELL_COORDINATOR_URL` | Default `http://127.0.0.1:9477`; use LAN IP if Cursor is remote |
| `BLACKWELL_API_TOKEN` | Bearer token if set in `%USERPROFILE%\.blackwell\config.json` |
| `BLACKWELL_OLLAMA_MODEL` | Model for duration estimates (default `qwen2.5:7b`) |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| MCP not connected | Run `uv sync`; confirm `blackwell-mcp.exe` exists under `.venv\Scripts\` |
| Coordinator not reachable | Run `.\scripts\start-coordinator.ps1` |
| No entry point found | Pass `entry_command="python train.py"` when activating |
| `/activate` missing | MCP not loaded — reload or restart Cursor |
