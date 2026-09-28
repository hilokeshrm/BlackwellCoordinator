# BlackwellCoordinator

A traffic controller for the team's shared **NVIDIA RTX PRO 6000 Blackwell** workstation.

- **Coordinator daemon + dashboard** (`coordinator/`): watches the GPU, runs a smart queue, starts jobs when the GPU is idle, pauses long jobs at a checkpoint so short ones can squeeze in, and shows live usage, ETAs and finish times.
- **Blackwell MCP** (`blackwell_mcp/`): one Cursor tool, **`activate`** (also available as the `/activate` slash prompt). It is the only way a project joins the coordinator. It installs checkpoint/resume, milestones, heartbeats and graceful stop into the project, registers it, and reports GPU availability and ETA.
- **Project SDK** (`blackwell_mcp/templates/blackwell_sdk.py`): a stdlib-only file that `activate` copies into each project.

```
Cursor ──/activate──► Blackwell MCP ──REST──► Coordinator daemon ──► job subprocesses (your train.py + blackwell_sdk)
                                                  │  ▲                          │
                                           nvidia-smi  └──── heartbeats / checkpoints / stop ◄┘
                                                  ▼
                                        Dashboard  http://<gpu-host>:9477/
```

## Setup (GPU host, once)

```powershell
cd C:\Users\Sirena Tech\Documents\BlackwellCoordinator
uv sync                                  # creates .venv with everything
.\scripts\install-autostart.ps1          # coordinator service: runs in the background, starts at logon, restarts on crash
.\scripts\install-desktop-app.ps1        # desktop app: Start menu entry + tray icon at login
.\scripts\install-cursor-mcp.ps1         # adds /activate to Cursor
.\scripts\start-jev.ps1                  # Open-Jev decision server (Docker, GPU); -Big for the 9B checkpoint
```

The daemon reads the real GPU via nvidia-smi and refuses to start without it; there is no simulated mode outside the test suite.

### Three ways in

| | What | How to open |
|---|---|---|
| **Coordinator service** | Background process that watches the GPU and runs the queue. No window. | Starts itself at logon (Task Scheduler task `BlackwellCoordinator`). Log: `%USERPROFILE%\.blackwell\coordinator.log`. |
| **Desktop app** | Native window with the full dashboard: start, schedule, pause, logs. Lives in the system tray; tooltip shows GPU load and the running job; Windows notifications when a job starts, finishes, fails or pauses and when a service goes down. Closing the window keeps it in the tray. | Start menu → **Blackwell Coordinator**, or the tray icon (click **^** next to the clock). Right-click the tray icon for *Open in browser*, *Start with Windows*, *Quit app*. Quitting the app never stops the coordinator. |
| **Website** | Same dashboard for teammates on other PCs. | `http://<gpu-host-ip>:9477/` (this PC: `http://192.168.88.3:9477/`). Allow the port through the firewall if needed. |

### Cursor (each teammate)

`.\scripts\install-cursor-mcp.ps1` adds the server to `%USERPROFILE%\.cursor\mcp.json` (keeping your other servers, with a backup). Pass `-Url http://<gpu-host-ip>:9477` when Cursor runs on another PC. The resulting entry:

```json
{
  "mcpServers": {
    "blackwell-coordinator": {
      "command": "C:\\Users\\Sirena Tech\\Documents\\BlackwellCoordinator\\.venv\\Scripts\\python.exe",
      "args": ["-m", "blackwell_mcp.server"],
      "env": { "BLACKWELL_COORDINATOR_URL": "http://127.0.0.1:9477", "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

Then in Cursor: **Settings → MCP** (or *Tools & Integrations*), check that `blackwell-coordinator` shows a green dot (toggle it off/on or reload the window if it doesn't), and type **`/activate`** in the agent chat inside a project folder. The generated `blackwell-mcp.exe` launcher is blocked by Windows Application Control on this PC, which is why the config calls `python.exe -m blackwell_mcp.server`.

Projects must live on the GPU host (open them in Cursor directly on the machine or via Remote-SSH), because the coordinator runs the job from that folder.

## Team workflow

1. Open the project in Cursor and type **`/activate`** (or ask the agent to "activate this project for Blackwell, owner alice, about 2 hours").
2. `activate` scans the project (entry script, framework, epochs from config), installs `blackwell_sdk.py`, writes `.blackwell/profile.json` and `.cursor/rules/blackwell.mdc`, registers the project, and returns **agent tasks** — framework-specific edits (plain PyTorch, Hugging Face `Trainer`, Lightning) that add resume, progress and graceful stop, wrapped in `# BLACKWELL:START/END` markers.
3. The Cursor agent applies the tasks and calls `activate` again; the status becomes **complete** and the card shows *resumable*.
4. On the dashboard: **Start now** (greyed out while the GPU is busy or another job runs) or **Schedule** — *when the GPU is free*, *tonight*, *tomorrow morning* or a specific time, plus priority and window.
5. Re-run `activate` whenever the entry command, epochs or duration change. If the last run failed, `activate` shows the tail of its log so the agent can fix it.

### Training jobs vs deployed services

`activate` registers two kinds of project:

| | Training job | Deployed service |
|---|---|---|
| Examples | finetunes, pretraining, benchmarks | AgentOS (m3), an inference API, a chat UI |
| How it's detected | a training script, no compose files | docker compose files and no training script, or `kind="service"` |
| Code changes | SDK for checkpoint/resume/progress | none |
| Run by the coordinator | yes: queued, started, paused, resumed | never: it runs on its own |
| On the dashboard | job card with ETA and Start / Schedule | service card with UP/DOWN, latency, GPU memory |
| Scheduling effect | one job at a time, 10 % idle rule | its GPU **load** is ignored by the idle rule; its **memory** counts, and stays reserved while it is down |

AgentOS is registered as a service: health check `https://localhost:7070/service1`, GPU processes `llama-server` / `ollama` (the host Ollama it uses). To register another service, open its folder in Cursor and run `/activate` as a service, optionally passing `health_url`, `process_match` and `expected_vram_gb`.

## How the coordinator decides

Deterministic rules first; an LLM only breaks genuine ties.

| Layer | What it does |
|---|---|
| Hard rules | Starts a job only when GPU util < **10 %** for **60 s** (grace), nothing else running, and util + the job's expected load ≤ 100 %. External usage (someone running outside the coordinator) blocks auto-start too. |
| Gap filling | Jobs > 8 h or `night_only` are *anchors* that take the **22:00–07:00** night window. By day, short jobs that finish before tonight go first. A lone long `anytime` job may use an idle daytime GPU. |
| Preemption | By day, if a resumable long job is running and a short job (≤ 4 h, same or higher priority) is queued, the long job is asked to checkpoint and stop, the short job runs, then the long job **resumes automatically** from its checkpoint. |
| Open-Jev picks | Whenever more than one job is allowed to start, **Open-Jev** chooses among them (the same non-generative decision server m3 uses; it can only answer with an offered option and returns a calibrated probability). It also confirms each daytime preemption (pause / keep). Its pick is used only when p ≥ 0.55 and it doesn't jump a higher-priority job; otherwise the heuristic order wins. |
| Fallbacks | Jev offline → local Ollama (`qwen2.5:7b`) picks among the same options → heuristic order. The dashboard shows which one decided each start. |
| Local LLM | `activate` asks Ollama for a duration estimate only when none was given. |

ETAs use observed speed once a run has progressed (blended with the estimate), so *Remaining* and *Finishes* get sharper as the job runs.

### Stop / resume protocol

The coordinator never kills a resumable job outright. It sets a flag returned on the next heartbeat and writes `.blackwell/STOP`; the SDK's `bw.should_stop` turns true, the script checkpoints and calls `bw.exit_paused()`. SIGINT/SIGTERM/CTRL-BREAK trigger the same path. A hard kill happens only if the job ignores the request for 120 s. If the coordinator restarts, running jobs are asked to stop and re-queued to resume.

## Dashboard

"Thermal" theme: colour tracks GPU load — teal when idle, amber when warm, ember orange when busy, rose when flat out — on the gauge, the utilisation trace, the header underline and the ambient background. Running jobs glow ember (they heat the GPU), services are lavender, free memory is teal. Motion is functional: numbers count to new values, bars and memory segments grow, the trace draws in and its live head pulses, cards flash when their status changes, down services pulse red. Everything respects the OS "reduce motion" setting.

- **Overview**: live GPU trace (5 m / 30 m / 2 h / 24 h) with the 10 % auto-start band; *Can a job start?* verdict naming the process that's blocking it; **GPU memory map** split by owner (training jobs, each service, WSL/Docker VM, untracked, desktop), memory reserved for down services, and how much a new job can use right now.
- **On the GPU right now**: every process holding GPU memory with its memory and GPU %, labelled with who it belongs to. Read from the same Windows counters as Task Manager (nvidia-smi can't see per-process memory under WDDM).
- **Deployed services**: UP/DOWN with a pulse, uptime, last check and latency, GPU memory and load, matched processes. The Services tab shows "1 down" in red when something is down.
- **Training jobs**: 36-hour plan with night windows, and one card per job with the finish time up front, progress, remaining / elapsed, expected GPU and memory, resumable badge, Start now / Schedule / Pause at checkpoint / Remove from queue.
- **Activity**: every start, pause, preemption, service outage and which rule or model decided it.
- **Logs** (lock icon): every run's output, SDK checkpoints and milestones, scheduler decisions, run history, CSV export. Password protected; ask the team lead.

## Configuration

Defaults live in `coordinator/config.py`; override in `%USERPROFILE%\.blackwell\config.json` (e.g. `{"night_start_hour": 23, "idle_threshold_percent": 20}`) or env vars: `BLACKWELL_PORT`, `BLACKWELL_API_TOKEN` (required bearer token for registration), `BLACKWELL_IDLE_GRACE`, `BLACKWELL_THRESHOLD`, `BLACKWELL_DECIDER` (`auto|heuristic|jev|ollama`), `JEV_URL`, `JEV_MIN_PROBABILITY`, `BLACKWELL_OLLAMA_MODEL`. Data: `%USERPROFILE%\.blackwell\coordinator.db` (logs kept 30 days).

## API (`/api/v1`)

`GET /health` · `GET /gpu` · `GET /gpu/history?minutes=` · `GET /state` · `WS /stream` · `GET|POST /projects` · `GET /projects/{id}` · `POST /projects/{id}/start|schedule|pause|cancel|heartbeat|logs` · `GET /queue/timeline` · `POST /scheduler/tick` · `POST /admin/login` · `GET /admin/logs|logs.csv|runs` · `DELETE /admin/projects/{id}` · `POST /projects/{id}/start?force=true` (admin override).

## Open-Jev

`jev-router/` holds the same Dockerfile and entrypoint as m3's `agent-os-m2-final/docker/jev-router`, published on `127.0.0.1:8791` and sharing m3's `agentos_jev_models` weights volume.

```powershell
.\scripts\start-jev.ps1         # 2B, ~6 GB VRAM (recommended: it stays loaded next to training jobs)
.\scripts\start-jev.ps1 -Big    # 9B, ~20 GB VRAM
```

Point the coordinator elsewhere with `JEV_URL` (e.g. m3's router if you publish its port). The coordinator warms Jev on startup (the first question compiles kernels, ~25 s), re-probes every 5 minutes while it is offline, and shows its status in the dashboard's side panel.

## Tests

```powershell
.\scripts\run-tests.ps1
```

Unit tests cover the scheduler (gap fill, night anchors, priority, preemption, ETA) and the Open-Jev layer (API format, confidence threshold, priority guard, offline fallback). End-to-end tests start a coordinator with a GPU test double and drive throwaway sample projects (created in a temp dir, never on the real GPU) through MCP `activate` → start → greyed-out Start → completion, GPU-busy blocking and auto-start, pause → checkpoint → resume, the 33 h / 2 h preemption case, timeline, admin lock and restart recovery.

## Security notes

Internal-tool grade. The admin password is hardcoded in one innocuously named source module (not in any env or config file) and checked server-side. That is hiding, not real security: put the dashboard behind a VPN or the office LAN, set `BLACKWELL_API_TOKEN` if the port is reachable by others, and never expose port 9477 directly on a public IP.
