# BlackwellCoordinator — Product Requirements Document (PRD)

**Version:** 1.0  
**Date:** 2026-09-28  
**Status:** Plan / Ready for Implementation  
**Audience:** Engineering team + AI implementation agent (Claude)  
**Purpose:** Single source of truth for building an internal GPU coordination platform for a 5–6 person team sharing one Blackwell system.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Goals & Non-Goals](#3-goals--non-goals)
4. [Users & Personas](#4-users--personas)
5. [System Overview](#5-system-overview)
6. [Architecture](#6-architecture)
7. [Component Specifications](#7-component-specifications)
8. [MCP Server — `activate` (Single Entry Point)](#8-mcp-server--activate-single-entry-point)
9. [Scheduling Engine (JEV + Heuristics + Local LLM)](#9-scheduling-engine-jev--heuristics--local-llm)
10. [Project Instrumentation Contract](#10-project-instrumentation-contract)
11. [Coordinator Daemon](#11-coordinator-daemon)
12. [Web UI / Dashboard](#12-web-ui--dashboard)
13. [API Specification](#13-api-specification)
14. [Data Models & Persistence](#14-data-models--persistence)
15. [Security & Access Control](#15-security--access-control)
16. [Remote Access & Public IP Scenario](#16-remote-access--public-ip-scenario)
17. [Demo Projects & Test Plan](#17-demo-projects--test-plan)
18. [Implementation Phases](#18-implementation-phases)
19. [Tech Stack Recommendations](#19-tech-stack-recommendations)
20. [Repository Structure](#20-repository-structure)
21. [Future Enhancements (Post-MVP)](#21-future-enhancements-post-mvp)
22. [Open Questions & Decisions Needed](#22-open-questions--decisions-needed)
23. [Appendix A: Example Schedules](#23-appendix-a-example-schedules)
24. [Appendix B: MCP `activate` Return Payload Schema](#24-appendix-b-mcp-activate-return-payload-schema)
25. [Appendix C: Cursor Integration Patterns](#25-appendix-c-cursor-integration-patterns)

---

## 1. Executive Summary

**BlackwellCoordinator** is an internal tool that sits on the shared Blackwell GPU machine and acts as a **traffic controller** for training, finetuning, and benchmark jobs across ~5–6 teammates who all use the same hardware but work on different projects in Cursor.

The system has three pillars:

| Pillar | Role |
|--------|------|
| **Coordinator Daemon** | Background service: GPU monitoring, queue execution, checkpoint-aware job lifecycle |
| **Web Dashboard** | Modern UI: queue, schedule, GPU graphs, ETAs, locked admin logs |
| **Blackwell MCP** | Cursor-facing integration: **one tool — `activate`** — that registers a project, instruments it for resume/logging/milestones, and connects it to the coordinator |

**Core behavior:** When GPU utilization drops below **25%** for a sustained grace period, the coordinator picks the best next job from the queue — not naïvely FIFO, but using **gap-filling scheduling** (e.g., run a 2-hour finetune now, defer a 33-hour training run until tonight).

**Decision stack:** Deterministic heuristics first → **JEV** (TypeSafe System One model) for structured scheduling decisions → local LLM only when prose/code generation is required (project instrumentation during `activate`).

---

## 2. Problem Statement

### Current Pain Points

1. **Resource conflicts:** One person's training randomly stops when another starts work on the same GPU.
2. **No resume:** Many projects lack checkpoint/resume — a stopped run means restarting from scratch (33+ hours lost).
3. **No visibility:** Team cannot see who is using the GPU, what's queued, or when jobs will finish.
4. **Poor scheduling:** Sequential "first come first served" wastes daytime gaps and doesn't prioritize short jobs before long overnight runs.
5. **Inconsistent logging:** No unified log stream; debugging failures is painful.
6. **Remote testing:** Sometimes services run on a public IP and are tested from another PC — no central view of GPU state.

### Desired Outcome

A teammate opens their project in Cursor, runs **`activate`** via the Blackwell MCP, and the project becomes **coordinator-aware** (checkpoints, milestones, structured logs, queue registration). They then use the dashboard to **Start Now** or **Schedule**. The coordinator runs jobs safely, shows ETAs, and maximizes GPU utilization — especially overnight when the machine is usually idle.

---

## 3. Goals & Non-Goals

### Goals (MVP)

- [ ] Single MCP tool `activate` that fully onboards a project into the coordinator ecosystem
- [ ] GPU monitoring with real-time graph (utilization %, memory, power optional)
- [ ] Auto-start queued jobs when GPU < 25% (configurable threshold + grace period)
- [ ] Smart non-sequential scheduling: short jobs fill gaps before long jobs
- [ ] Night-window preference for long-running jobs (configurable)
- [ ] Per-project ETA: elapsed, remaining, projected finish date/time
- [ ] Checkpoint/resume injection for projects that lack it
- [ ] Milestone tracking and structured logging
- [ ] Dashboard: project cards, Start Now / Schedule, greyed-out Start when GPU busy
- [ ] Password-protected admin log viewer (password hardcoded obscurely — see §15)
- [ ] Two demo projects with full end-to-end test coverage
- [ ] Coordinator ↔ MCP live connection for GPU state and registration

### Non-Goals (MVP)

- Multi-machine cluster scheduling (single Blackwell host only)
- Kubernetes / Slurm integration
- Billing/chargeback (future)
- Preempting a running job to start another (no kill-and-switch in MVP — jobs run to checkpoint or completion)
- Mobile-native app (responsive web is sufficient)

---

## 4. Users & Personas

| Persona | Needs |
|---------|-------|
| **ML Engineer (Teammate)** | Activate project in Cursor, queue job, see ETA, resume after interruption |
| **Team Lead / You** | Full logs, queue overview, override schedule, see historical usage |
| **Remote Tester** | Read-only GPU dashboard via public IP (optional MVP stretch) |

**Team size:** 5–6 people  
**Primary IDE:** Cursor (100% of workflow)  
**Hardware:** Single Blackwell GPU system (assume 1 primary GPU; architecture should allow N GPUs later)

---

## 5. System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TEAMMATE WORKSTATIONS                              │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐                     │
│  │ Cursor + MCP │   │ Cursor + MCP │   │ Cursor + MCP │  ... (5-6 users)    │
│  │  activate()  │   │  activate()  │   │  activate()  │                     │
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘                     │
└─────────┼──────────────────┼──────────────────┼─────────────────────────────┘
          │                  │                  │
          │    MCP stdio / HTTP (local)         │
          ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     BLACKWELL GPU MACHINE (HOST)                             │
│                                                                              │
│  ┌─────────────────┐      REST + WebSocket      ┌─────────────────────────┐ │
│  │  Blackwell MCP  │◄──────────────────────────►│  Coordinator Daemon     │ │
│  │  (activate)     │                            │  - GPU poll (nvml)      │ │
│  └────────┬────────┘                            │  - Scheduler engine     │ │
│           │                                       │  - Job runner           │ │
│           │ instruments                           │  - SQLite persistence   │ │
│           ▼                                       └───────────┬─────────────┘ │
│  ┌─────────────────┐                                           │               │
│  │ Project dirs    │◄──────── blackwell-run wrapper ───────────┘               │
│  │ + .blackwell/   │                                                            │
│  └─────────────────┘                                                            │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │  Coordinator Web UI (Vite + React)  —  localhost:9477 (+ optional LAN)   ││
│  │  GPU graph | Queue | Project cards | Schedule | Locked logs              ││
│  └─────────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Architecture

### 6.1 High-Level Components

| Component | Language | Runs On | Responsibility |
|-----------|----------|---------|----------------|
| `coordinator-daemon` | Python 3.11+ | GPU host | GPU metrics, scheduling, process management |
| `coordinator-ui` | TypeScript (React + Vite) | GPU host (served by daemon or separate) | Dashboard |
| `blackwell-mcp` | TypeScript (MCP SDK) | Each dev machine OR GPU host | `activate` tool, coordinator API client |
| `scheduler-core` | TypeScript or Python | Library | Heuristics + JEV client + timeline builder |
| `coordinator-protocol` | TypeScript (shared types) | Library | OpenAPI / JSON schemas |
| `project-instrumentation` | Python + templates | Injected into projects | SDK, wrappers, checkpoint helpers |

### 6.2 Communication Patterns

| From | To | Protocol | Purpose |
|------|-----|----------|---------|
| UI | Daemon | WebSocket | Live GPU metrics, job status |
| UI | Daemon | REST | CRUD queue, schedule, logs |
| MCP | Daemon | REST | Register project, fetch GPU capacity |
| Daemon | Project | subprocess | `blackwell-run` → actual training command |
| Project | Daemon | HTTP heartbeat | Progress, milestones, log lines |
| Daemon | JEV API | HTTPS | Scheduling decisions |
| Daemon | Local LLM | HTTP (Ollama/vLLM) | Duration estimation fallback only |

### 6.3 Deployment Model

**MVP:** All services on the Blackwell host.

- Daemon starts on boot (systemd Windows Service or `pm2` / NSSM on Windows)
- UI embedded in daemon at `http://127.0.0.1:9477`
- MCP configured in each developer's Cursor `mcp.json` pointing to coordinator host URL

---

## 7. Component Specifications

### 7.1 Design Principles

1. **Cursor-first onboarding:** Projects enter the system only via MCP `activate`.
2. **Heuristics before LLM:** Use JEV for typed decisions; local LLM only when generating code or parsing unstructured configs.
3. **Fail safe:** Never start a second GPU job if utilization suggests an active process (>25% unless confirmed stale).
4. **Resume by default:** Every activated project must support graceful SIGINT/SIGTERM → checkpoint → resume.
5. **Observable:** Every job emits structured JSON logs and heartbeats.

---

## 8. MCP Server — `activate` (Single Entry Point)

> **Note:** In MCP, the single entry point is **one tool named `activate`**. Users invoke it from Cursor chat (colloquially "/activate"). There are **no other MCP tools** in MVP.

### 8.1 Tool Definition

```json
{
  "name": "activate",
  "description": "Register and instrument the current project for BlackwellCoordinator. Scans the project, adds checkpoint/resume/milestone/logging integration, creates a coordinator profile, registers with the daemon, and returns next steps for the Cursor agent.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "project_path": {
        "type": "string",
        "description": "Absolute path to project root. Defaults to Cursor workspace root."
      },
      "owner": {
        "type": "string",
        "description": "Teammate name or handle"
      },
      "estimated_duration_minutes": {
        "type": "number",
        "description": "Optional manual estimate. If omitted, MCP infers from config/history."
      },
      "priority": {
        "type": "string",
        "enum": ["low", "normal", "high", "critical"],
        "default": "normal"
      },
      "preferred_window": {
        "type": "string",
        "enum": ["anytime", "night_only", "daytime_only"],
        "default": "anytime"
      },
      "entry_command": {
        "type": "string",
        "description": "Optional override: shell command to run training (e.g. 'python train.py --config cfg.yaml')"
      }
    },
    "required": ["owner"]
  }
}
```

### 8.2 `activate` Execution Pipeline (7 Stages)

When `activate` is called, the MCP server orchestrates the following **internally** (still one tool from Cursor's perspective):

#### Stage 1 — Preflight
- Verify coordinator daemon reachable (`GET /health`)
- Read current GPU snapshot (`GET /gpu`)
- Check if project already activated (`.blackwell/profile.json` exists)

#### Stage 2 — Project Discovery
- Scan for: `train.py`, `finetune.py`, `main.py`, HuggingFace `Trainer`, PyTorch Lightning, shell scripts, `docker-compose`, configs
- Detect framework: `pytorch` | `huggingface` | `lightning` | `custom_script` | `unknown`
- Parse config files for epochs, batch size, dataset size → **duration estimate**

#### Stage 3 — Instrumentation Plan (returned to Cursor Agent)
MCP returns a **structured implementation plan** as tool result text. Cursor agent executes code changes:

| Injection | Description |
|-----------|-------------|
| `.blackwell/profile.json` | Project metadata, owner, entry command, estimates |
| `.blackwell/coordinator.yaml` | Coordinator SDK config (daemon URL, project ID) |
| `blackwell/runner.py` or `blackwell/runner.sh` | Wrapper: signals, heartbeat, milestone hooks |
| Checkpoint module | Framework-specific: save on SIGTERM, `--resume` flag |
| Logging shim | JSON lines → daemon log ingest endpoint |
| `.cursor/rules/blackwell.mdc` | Rule: always use coordinator for GPU jobs |
| Optional: patch `train.py` | Minimal invasive checkpoint/resume insertion |

**Important:** MCP does not silently rewrite code. It returns explicit instructions + file templates; **Cursor agent applies edits** (this matches user expectation: "ask cursor to make all the changes needed").

#### Stage 4 — Registration
- `POST /projects` to daemon with profile payload
- Receive `project_id`, queue slot

#### Stage 5 — GPU Capacity Report
- Return to user:
  - Current GPU % and whether **Start Now** would be allowed
  - Predicted GPU usage if this project starts (based on historical profile or default 90%)
  - Other running/queued jobs

#### Stage 6 — Readiness Verification
- MCP optionally runs dry-run: `blackwell-run --dry-run` to validate entry command

#### Stage 7 — Activation Summary
Return markdown summary:
- Project ID, registration status
- Instrumentation checklist (what was added / what agent still needs to do)
- Suggested next action: "Open dashboard → Start Now" or "Schedule for tonight"

### 8.3 MCP ↔ Daemon Configuration

```json
// ~/.cursor/mcp.json (each developer)
{
  "mcpServers": {
    "blackwell-coordinator": {
      "command": "node",
      "args": ["path/to/blackwell-mcp/dist/index.js"],
      "env": {
        "BLACKWELL_COORDINATOR_URL": "http://192.168.x.x:9477",
        "BLACKWELL_API_TOKEN": "team-shared-token"
      }
    }
  }
}
```

### 8.4 What `activate` Must NOT Do

- Expose separate tools for GPU, queue, logs (single tool only)
- Start training directly without user clicking Start Now / Schedule in UI (unless user explicitly passes `auto_start: true` in a future version — **not MVP**)

---

## 9. Scheduling Engine (JEV + Heuristics + Local LLM)

### 9.1 Decision Layer Architecture

```
                    ┌─────────────────────┐
                    │   Schedule Request   │
                    │ (queue + GPU state)  │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Layer 1: Hard Rules │  ← deterministic, always first
                    │  - GPU must be <25%   │
                    │  - Only 1 GPU job     │
                    │  - Stale process det. │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ Layer 2: Heuristics  │  ← timeline / gap-filling
                    │  - Gap insertion     │
                    │  - Night window      │
                    │  - Priority sort     │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │ ambiguous?     │ clear winner?   │
              ▼                ▼                 │
     ┌────────────────┐  ┌──────────────┐        │
     │ Layer 3: JEV   │  │ Pick winner  │        │
     │ choice/score   │  └──────────────┘        │
     └────────────────┘                          │
              │                                   │
              └───────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │ Layer 4: Local LLM   │  ← ONLY for:
                    │ (Ollama etc.)        │    - duration estimate
                    └─────────────────────┘    - parse unknown configs
```

### 9.2 Hard Rules (Layer 1)

| Rule | Default | Config Key |
|------|---------|------------|
| GPU util threshold to start | < 25% | `gpu.idle_threshold_percent` |
| Sustained idle before start | 60 seconds | `gpu.idle_grace_seconds` |
| Max concurrent GPU jobs | 1 | `scheduler.max_concurrent` |
| Stale job detection | No heartbeat 5 min | `job.heartbeat_timeout_seconds` |

**Start button greyed out in UI when:**
- GPU utilization ≥ 25% OR
- Another job status = `running` OR
- Predicted overlap: starting would exceed 100% GPU memory

### 9.3 Gap-Filling Heuristic (Layer 2)

**Problem:** Job A = 33 hours (night preferred). Job B = 2 hours (needed soon). Naïve FIFO wastes the afternoon.

**Algorithm: `buildTimeline(queue, now)`**

1. Sort candidates by `priority` DESC, then `deadline` ASC
2. Identify **anchor jobs**: duration > 8h OR `preferred_window = night_only`
3. Place anchor jobs into next available **night windows** (default 22:00–07:00 local)
4. For remaining short jobs (duration ≤ gap threshold, default 4h):
   - Find free gaps before next anchor start
   - Insert if `gap_duration >= job_duration + buffer` (buffer default 15 min)
5. Produce ordered execution plan with projected start/end timestamps

**Example:**

| Job | Duration | Window | Planned Start | Planned End |
|-----|----------|--------|---------------|-------------|
| finetune-B | 2h | anytime | Today 14:00 | Today 16:00 |
| train-A | 33h | night | Today 22:00 | Day+2 07:00 |

### 9.4 JEV Integration (Layer 3)

Use **TypeSafe JEV** via Vercel AI Gateway or TypeSafe SDK when heuristics produce ties or low-confidence choices.

**JEV input state (structured, minimal — avoid context rot):**

```json
{
  "gpu_util_percent": 12,
  "queue": [
    {"id": "p1", "duration_min": 120, "priority": "high", "owner": "alice"},
    {"id": "p2", "duration_min": 1980, "priority": "normal", "owner": "bob"}
  ],
  "current_time_local": "2026-09-28T14:00:00+05:30",
  "next_night_start": "2026-09-28T22:00:00+05:30",
  "running_job": null
}
```

**JEV questions:**

```typescript
// Pseudocode
decide({
  model: "typesafe-ai/jev",
  state,
  questions: {
    next_job: choice({
      instructions: "Which queued job should start now given GPU is idle and gap-filling rules?",
      options: { /* project ids */ }
    }),
    defer_long_job: boolean({
      instructions: "Should the 33h job wait for tonight even though GPU is idle now?"
    }),
    confidence_gate: score({
      instructions: "How confident are you this schedule avoids teammate conflicts?",
      levels: { low: 0, medium: 0.5, high: 1.0 }
    })
  }
})
```

**Fallback if JEV unavailable:** Pure heuristic + priority sort.

### 9.5 Local LLM Usage (Layer 4 — Limited)

| Use Case | Use LLM? |
|----------|----------|
| Pick next job from 2 candidates | **No** → JEV |
| Estimate duration from README + config | **Yes** if no historical data |
| Generate checkpoint injection code | **Yes** (Cursor agent during activate) |
| Parse ambiguous shell script entry | **Yes** |
| Real-time GPU start/stop | **No** → heuristics |

---

## 10. Project Instrumentation Contract

Every activated project MUST have:

### 10.1 Directory Layout

```
my-project/
├── .blackwell/
│   ├── profile.json          # Coordinator registration
│   ├── coordinator.yaml      # Daemon connection
│   ├── milestones.json       # Checkpoint/milestone state
│   └── checkpoints/          # Saved model checkpoints
├── blackwell/
│   ├── runner.py             # Entry wrapper
│   ├── heartbeat.py          # Progress reporter
│   └── signals.py            # SIGTERM/SIGINT handlers
└── ... (existing project files)
```

### 10.2 `profile.json` Schema

```json
{
  "project_id": "uuid",
  "name": "my-finetune",
  "owner": "alice",
  "framework": "huggingface",
  "entry_command": "python train.py --config configs/lora.yaml",
  "estimated_duration_minutes": 120,
  "estimated_gpu_utilization_percent": 85,
  "priority": "normal",
  "preferred_window": "anytime",
  "supports_resume": true,
  "activated_at": "2026-09-28T10:00:00Z",
  "coordinator_version": "1.0.0"
}
```

### 10.3 Runner Behavior (`blackwell-run`)

1. On start: register run with daemon → `run_id`
2. Every 30s: heartbeat `{ epoch, step, loss, gpu_mem, percent_complete }`
3. On SIGTERM/SIGINT: trigger checkpoint callback → update `milestones.json` → exit 0
4. On resume: read latest milestone → pass `--resume-from` to training script
5. On completion: notify daemon → status `completed`

### 10.4 Milestone Schema

```json
{
  "last_checkpoint": "2026-09-28T18:30:00Z",
  "checkpoint_path": ".blackwell/checkpoints/epoch_12.pt",
  "epoch": 12,
  "total_epochs": 50,
  "percent_complete": 24.0,
  "can_resume": true
}
```

### 10.5 Structured Log Format

JSON Lines to daemon `POST /projects/{id}/logs`:

```json
{"ts":"2026-09-28T18:30:01Z","level":"INFO","run_id":"...","message":"epoch 12 loss=0.042","metrics":{"loss":0.042,"lr":1e-5}}
```

---

## 11. Coordinator Daemon

### 11.1 Responsibilities

- Poll GPU every **2 seconds** via `pynvml` (fallback: `nvidia-smi` parse)
- Maintain job queue in SQLite
- Execute scheduler tick every **10 seconds** when idle
- Spawn/kill subprocesses for jobs (kill = graceful SIGTERM only)
- Aggregate logs, serve WebSocket stream
- Expose REST API for UI and MCP

### 11.2 Job State Machine

```
registered → queued → scheduled → starting → running → checkpointing → paused
                ↓                                    ↓
              cancelled                          completed
                                                   ↓
                                                 failed
```

### 11.3 Process Management

- Start command: `blackwell-run --project-id {id} --run-id {run_id}`
- Working directory: project root
- Environment: inherit + `BLACKWELL_RUN_ID`, `BLACKWELL_PROJECT_ID`
- Log capture: stdout/stderr → structured log pipeline

### 11.4 ETA Calculation

```
remaining = estimated_duration * (1 - percent_complete)
         OR (total_epochs - current_epoch) * avg_epoch_duration

finish_at = now + remaining + queued_ahead_duration (if not running yet)
```

Display in UI:
- **Elapsed:** 2h 14m
- **Remaining:** ~1h 46m (confidence: medium)
- **Projected finish:** Mon Sep 28, 2026 8:45 PM IST

---

## 12. Web UI / Dashboard

### 12.1 Visual Design Direction

**Theme:** Dark, modern, "mission control" aesthetic

| Token | Value |
|-------|-------|
| Background | `#0B0F17` (deep navy-black) |
| Surface | `#141B2D` |
| Primary accent | `#3B82F6` (electric blue) |
| Success / running | `#22C55E` |
| Warning / queued | `#F59E0B` |
| Error / failed | `#EF4444` |
| Text primary | `#F1F5F9` |
| Text muted | `#94A3B8` |
| Font | `Inter` + `JetBrains Mono` for logs |

**Components:** shadcn/ui + Recharts for GPU line graph + framer-motion for card transitions

### 12.2 Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  ⚡ BlackwellCoordinator          GPU: 12%  │  Queue: 3  │  🌙  │
├──────────────────────────────────────────────────────────────────┤
│  ┌─ GPU Utilization (live) ──────────────────────────────────┐  │
│  │     📈 Line chart — last 30 min, 1s resolution             │  │
│  │     [====12%====                    ]  VRAM: 4.2/24 GB      │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌─ Active Job ────────────────────────────────────────────────┐  │
│  │  long-training-demo  │  alice  │  Epoch 12/50  │  ETA 31h   │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌─ Project Queue ─────────────────────────────────────────────┐  │
│  │ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐            │  │
│  │ │ Profile Box │ │ Profile Box │ │ Profile Box │            │  │
│  │ │ quick-ft    │ │ benchmark-x │ │ train-big   │            │  │
│  │ │ bob · 2h    │ │ eve · 45m   │ │ alice · 33h │            │  │
│  │ │ ▓▓▓░░ 60%   │ │ queued      │ │ scheduled   │            │  │
│  │ │ Finish: 4pm │ │ Finish: 5pm │ │ Finish: Wed │            │  │
│  │ │[Start Now]  │ │[Start Now]  │ │[Schedule ▼] │            │  │
│  │ │ (greyed)    │ │             │ │             │            │  │
│  │ └─────────────┘ └─────────────┘ └─────────────┘            │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌─ Timeline (Gantt) ──────────────────────────────────────────┐  │
│  │  now ▼   [quick-ft==][----train-big------------------------] │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  🔒 Admin Logs (locked)                                          │
└──────────────────────────────────────────────────────────────────┘
```

### 12.3 Project Card Fields

- Project name + directory path (truncated)
- Owner avatar/initials
- Status badge: `idle` | `queued` | `scheduled` | `running` | `paused` | `completed` | `failed`
- Progress bar (% complete from milestones)
- Time block: elapsed | remaining | projected finish (with timezone)
- GPU estimate when running: "~85% util expected"
- Actions:
  - **Start Now** — disabled/greyed when GPU busy or another job running
  - **Schedule** — opens modal: datetime picker, night preset, priority
  - **Pause** (running jobs) — triggers graceful checkpoint
  - **Cancel** (queued only)

### 12.4 GPU Graph

- Real-time line chart: GPU util % (0–100)
- Secondary line: VRAM % (optional toggle)
- Time ranges: 5m | 30m | 2h | 24h
- WebSocket push every 2s
- Hover tooltip with timestamp + exact values

### 12.5 Locked Admin Logs Panel

- 🔒 icon in footer/sidebar
- Click → password modal
- On success: full-screen log viewer
  - All projects, all runs
  - Filter by level, project, owner, date
  - Raw JSONL + pretty view
  - Export CSV

**Password storage (per user requirement):**
- Password value: known to the team lead (not written in any document)
- **NOT in `.env`** or any config file
- Hardcoded in one innocuously named source module; its location is intentionally not documented
- Checked server-side; the logs API requires the resulting admin session (not cryptographically strong — acceptable for an internal team tool)

### 12.6 Schedule Modal

- Date/time picker
- Quick presets: "Tonight 10 PM", "Tomorrow morning", "Next weekend"
- Show projected queue impact: "If scheduled here, finishes Wed 7 AM"
- Conflict warning if overlaps running job

---

## 13. API Specification

**Base URL:** `http://{host}:9477/api/v1`

### 13.1 Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Daemon alive |
| GET | `/gpu` | Current GPU metrics + history tail |
| GET | `/gpu/stream` | WebSocket live metrics |
| GET | `/projects` | List all registered projects |
| POST | `/projects` | Register project (MCP activate) |
| GET | `/projects/{id}` | Project detail + ETA |
| PATCH | `/projects/{id}` | Update schedule/priority |
| POST | `/projects/{id}/start` | Start now (respects GPU rules) |
| POST | `/projects/{id}/schedule` | Set scheduled start time |
| POST | `/projects/{id}/pause` | Graceful pause |
| POST | `/projects/{id}/cancel` | Cancel queued |
| POST | `/projects/{id}/heartbeat` | Job progress (from runner) |
| POST | `/projects/{id}/logs` | Ingest log line |
| GET | `/projects/{id}/logs` | Fetch logs (auth for admin) |
| GET | `/queue/timeline` | Computed schedule timeline |
| POST | `/scheduler/tick` | Manual scheduler run (debug) |
| GET | `/admin/logs` | All logs (requires admin session cookie) |

### 13.2 Example: GPU Response

```json
{
  "timestamp": "2026-09-28T14:00:00+05:30",
  "gpus": [{
    "index": 0,
    "name": "NVIDIA ...",
    "utilization_percent": 12,
    "memory_used_mb": 4300,
    "memory_total_mb": 24576,
    "temperature_c": 45,
    "power_w": 85
  }],
  "can_start_new_job": true,
  "reason_if_blocked": null
}
```

---

## 14. Data Models & Persistence

**Database:** SQLite at `~/.blackwell/coordinator.db`

### 14.1 Tables

**projects**
| Column | Type |
|--------|------|
| id | UUID PK |
| name | TEXT |
| path | TEXT UNIQUE |
| owner | TEXT |
| framework | TEXT |
| entry_command | TEXT |
| estimated_duration_min | INT |
| priority | TEXT |
| preferred_window | TEXT |
| status | TEXT |
| created_at | DATETIME |

**runs**
| Column | Type |
|--------|------|
| id | UUID PK |
| project_id | FK |
| status | TEXT |
| started_at | DATETIME |
| finished_at | DATETIME |
| exit_code | INT |
| percent_complete | REAL |

**schedule**
| Column | Type |
|--------|------|
| project_id | FK |
| scheduled_start | DATETIME |
| scheduled_end | DATETIME (computed) |
| planner_version | TEXT |

**gpu_samples**
| Column | Type |
|--------|------|
| ts | DATETIME |
| util_percent | REAL |
| mem_used_mb | INT |

**logs**
| Column | Type |
|--------|------|
| id | INTEGER PK |
| project_id | FK |
| run_id | FK |
| ts | DATETIME |
| level | TEXT |
| message | TEXT |
| json | TEXT |

---

## 15. Security & Access Control

| Concern | MVP Approach |
|---------|--------------|
| API auth | Shared bearer token in MCP config (`BLACKWELL_API_TOKEN`) |
| UI admin logs | Obfuscated hardcoded password (see §12.5) |
| Network | Bind localhost by default; `--allow-lan` for team subnet |
| Public IP | Read-only dashboard mode (no start/stop) — stretch goal |
| Secrets in projects | MCP never commits `.env`; instrumentation uses env vars |

**Warning for team:** Hardcoded password is obfuscation, not security. Sufficient for internal trusted team; upgrade to proper auth in v2.

---

## 16. Remote Access & Public IP Scenario

Some teammates test services on public IP from another PC.

**MVP:**
- Coordinator UI accessible on LAN IP (e.g., `http://192.168.1.50:9477`)
- Optional nginx reverse proxy with basic auth for remote read-only view

**Dashboard from remote PC:**
- See GPU graph, queue, ETAs (read-only)
- Cannot start jobs unless authenticated (future)

**MCP from remote:** MCP runs on developer machine; points to coordinator URL (LAN or VPN). Public IP only if VPN/firewall configured — document in setup guide.

---

## 17. Demo Projects & Test Plan

### 17.1 Demo Project 1: `demo-quick-finetune`

**Purpose:** Validates gap-filling (short job runs before long job)

| Property | Value |
|----------|-------|
| Simulated duration | 10 minutes real time (represents 2h production) |
| Epochs | 20 |
| Checkpoint | Every 5 epochs |
| Framework | Pure Python mock (no real GPU needed for CI) |
| GPU mock | `--mock-gpu` flag sleeps instead of CUDA |

**Test scenarios:**
1. `activate` → instruments project → registers
2. Start Now while GPU idle → runs to completion
3. SIGTERM mid-run → resumes from checkpoint
4. Scheduled behind 33h job → scheduler inserts in afternoon gap

### 17.2 Demo Project 2: `demo-long-training`

**Purpose:** Validates overnight scheduling + long ETA

| Property | Value |
|----------|-------|
| Simulated duration | 30 minutes real time (represents 33h) |
| Epochs | 100 |
| preferred_window | `night_only` |
| Checkpoint | Every epoch |

**Test scenarios:**
1. `activate` → defers to night window when short job in queue
2. ETA displays multi-day finish time
3. Pause → checkpoint → resume across daemon restart

### 17.3 Test Matrix

| # | Test | Component | Pass Criteria |
|---|------|-----------|---------------|
| T1 | Daemon health | daemon | `/health` 200 |
| T2 | GPU poll | daemon | Metrics update every 2s |
| T3 | MCP activate | mcp | Project registered, files created |
| T4 | Start greyed | ui | Start disabled when GPU >25% |
| T5 | Gap schedule | scheduler | 2h job before 33h night job |
| T6 | JEV fallback | scheduler | Works without JEV key (heuristic) |
| T7 | Heartbeat | runner | Progress updates in UI |
| T8 | SIGTERM resume | runner | Restarts from checkpoint |
| T9 | Admin logs lock | ui | Wrong password rejected |
| T10 | WebSocket graph | ui | Chart updates live |
| T11 | Timeline API | scheduler | Matches expected Gantt |
| T12 | E2E both demos | all | Full activate → run → complete |

### 17.4 Test Infrastructure

- `MockGpuProvider` for CI (no NVIDIA hardware)
- `pytest` for daemon + scheduler
- `vitest` for MCP + UI units
- Playwright for UI E2E
- GitHub Actions or local `npm run test:all`

---

## 18. Implementation Phases

### Phase 0 — Scaffold (Day 1)
- [ ] Initialize monorepo structure
- [ ] Shared types package
- [ ] Docker-compose optional for dev
- [ ] README with setup

### Phase 1 — Daemon Core (Days 2–4)
- [ ] GPU polling (real + mock)
- [ ] SQLite schema + migrations
- [ ] REST API skeleton
- [ ] WebSocket GPU stream
- [ ] Job subprocess runner

### Phase 2 — Scheduler (Days 4–6)
- [ ] Heuristic timeline builder
- [ ] Gap-filling algorithm
- [ ] JEV client integration (optional key)
- [ ] Scheduler tick loop
- [ ] Unit tests for schedule scenarios (Appendix A)

### Phase 3 — MCP `activate` (Days 6–9)
- [ ] MCP server scaffold
- [ ] Project scanner
- [ ] Instrumentation templates
- [ ] Registration flow
- [ ] Cursor rule generation
- [ ] Integration test with demo project 1

### Phase 4 — Project Instrumentation (Days 8–10)
- [ ] `blackwell-run` Python package
- [ ] Heartbeat + signal handlers
- [ ] Milestone persistence
- [ ] Inject into both demo projects

### Phase 5 — Web UI (Days 10–14)
- [ ] Dashboard layout + theme
- [ ] GPU Recharts graph
- [ ] Project cards + Start/Schedule
- [ ] Greyed Start logic
- [ ] ETA display
- [ ] Gantt timeline view
- [ ] Locked admin logs

### Phase 6 — Integration & Demo Testing (Days 14–16)
- [ ] E2E both demo projects
- [ ] Team scenario simulation (6 users, conflicting queues)
- [ ] Bug fixes
- [ ] Setup documentation

### Phase 7 — Polish (Days 16–18)
- [ ] Windows service / auto-start
- [ ] Error handling + notifications
- [ ] Performance tuning
- [ ] Final QA against test matrix

**Estimated total:** ~3 weeks for 1 developer (or ~1.5 weeks with parallel work)

---

## 19. Tech Stack Recommendations

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Daemon | Python 3.11 + FastAPI | Best GPU libs (`pynvml`), ML ecosystem |
| Scheduler lib | Python (same repo) | Co-locate with daemon |
| UI | React 18 + Vite + TypeScript | Fast dev, rich chart ecosystem |
| UI components | shadcn/ui + Tailwind | Modern look, customizable |
| Charts | Recharts | Line graphs, responsive |
| MCP | `@modelcontextprotocol/sdk` TypeScript | Cursor standard |
| DB | SQLite + SQLAlchemy or `better-sqlite3` via Python | Zero-config local |
| Real-time | FastAPI WebSockets | Native, simple |
| JEV | Vercel AI SDK `decide()` + `typesafe-ai/jev` | Structured fast decisions |
| Local LLM | Ollama HTTP API | Optional duration parsing |
| Cursor automation | Cursor SDK (optional v2) | Daemon-triggered agent prompts |
| Testing | pytest + vitest + Playwright | Full stack |
| Packaging | `npm workspaces` + `uv`/`poetry` for Python | Monorepo |

---

## 20. Repository Structure

```
BlackwellCoordinator/
├── apps/
│   ├── coordinator-daemon/          # Python FastAPI
│   │   ├── src/
│   │   │   ├── main.py
│   │   │   ├── gpu/
│   │   │   ├── scheduler/
│   │   │   ├── jobs/
│   │   │   ├── api/
│   │   │   └── db/
│   │   ├── tests/
│   │   └── pyproject.toml
│   │
│   ├── coordinator-ui/                # React dashboard
│   │   ├── src/
│   │   │   ├── components/
│   │   │   ├── pages/
│   │   │   ├── hooks/
│   │   │   └── lib/internal/        # obfuscated auth
│   │   └── package.json
│   │
│   └── blackwell-mcp/               # MCP server
│       ├── src/
│       │   ├── index.ts
│       │   ├── activate/
│       │   │   ├── scanner.ts
│       │   │   ├── instrumenter.ts
│       │   │   └── registrar.ts
│       │   └── coordinator-client.ts
│       └── package.json
│
├── packages/
│   ├── coordinator-protocol/        # OpenAPI + shared JSON schemas
│   └── blackwell-runner/            # Python pip package for projects
│       ├── blackwell/
│       └── pyproject.toml
│
├── demo-projects/
│   ├── demo-quick-finetune/
│   │   ├── train.py
│   │   └── README.md
│   └── demo-long-training/
│       ├── train.py
│       └── README.md
│
├── docs/
│   ├── PRD-BlackwellCoordinator.md  # this file
│   ├── SETUP.md
│   └── ARCHITECTURE.md
│
├── scripts/
│   ├── install-windows-service.ps1
│   └── run-all-tests.sh
│
├── .cursor/
│   └── mcp.json.example
│
├── package.json                     # npm workspaces root
└── README.md
```

---

## 21. Future Enhancements (Post-MVP)

| Feature | Value |
|---------|-------|
| **Cursor SDK bridge** | Daemon sends agent prompt: "Start project X" when schedule fires |
| Slack/Discord webhooks | Notify on job start/complete/fail |
| Multi-GPU support | Per-GPU queues |
| Preemption with checkpoint | Pause long job for urgent short job if checkpoint fresh |
| Historical analytics | Utilization heatmap, per-person usage |
| Job dependencies | "Run benchmark after finetune completes" |
| Resource reservations | "Reserve GPU 2–4 PM for alice" |
| Proper RBAC | OAuth or team login instead of hardcoded password |
| Project templates | One-click activate for common HF/LLaMA configs |
| Automatic stale process cleanup | Detect orphan GPU processes |
| Email digest | Morning queue summary |

---

## 22. Open Questions & Decisions Needed

| # | Question | Recommendation | Needs User Input? |
|---|----------|----------------|-------------------|
| 1 | Exact night window hours? | 22:00–07:00 IST | Confirm timezone |
| 2 | GPU count on Blackwell system? | Assume 1, design for N | Yes |
| 3 | JEV API key / Vercel Gateway? | Optional; heuristic fallback | Provide key if available |
| 4 | Local LLM endpoint? | Ollama `localhost:11434` | Which models installed? |
| 5 | Windows vs Linux host? | User on Windows 10 | Confirm GPU host OS |
| 6 | Allow manual override Start when GPU busy? | Team lead only, behind admin lock | Preference? |
| 7 | Public IP exposure? | VPN recommended | Network setup |
| 8 | Max log retention? | 30 days default | Disk constraints |

---

## 23. Appendix A: Example Schedules

### Scenario 1: Gap-filling (canonical)

**Input @ 2:00 PM Monday:**
- GPU: 8% util, no running job
- Queue: `[finetune-2h (high), train-33h (normal, night)]`

**Output plan:**
1. Start `finetune-2h` at 2:00 PM → ends 4:00 PM
2. Start `train-33h` at 10:00 PM → ends ~7:00 AM Wednesday

### Scenario 2: GPU busy

**Input:**
- GPU: 78% util
- Running: `train-33h` at epoch 5/50

**Output:**
- All **Start Now** buttons greyed
- Queue shows waiting jobs with ETA including current run remaining

### Scenario 3: Interrupted training

**Input:**
- `train-33h` killed at epoch 20
- Milestone: checkpoint exists

**Output:**
- Status → `paused`
- UI shows **Resume** button
- On resume: scheduler picks slot (or immediate if GPU idle)

---

## 24. Appendix B: MCP `activate` Return Payload Schema

The tool returns markdown + embedded JSON for the Cursor agent:

```markdown
## Blackwell Activation Report

**Status:** partial | complete | failed
**Project ID:** `uuid`
**Coordinator URL:** http://192.168.1.50:9477

### GPU Status
- Utilization: 12% — **Start Now allowed**
- VRAM: 4.2 / 24 GB

### Instrumentation Checklist
- [x] Created `.blackwell/profile.json`
- [x] Created `blackwell/runner.py`
- [ ] **Agent action required:** Patch `train.py` to call `BlackwellCheckpoint.on_epoch_end()`
- [ ] **Agent action required:** Add `--resume` argparse flag

### Implementation Plan (for Cursor Agent)
<detailed steps>

### Registration
- Registered with coordinator: yes
- Queue position: 2

### Suggested Next Step
Open http://192.168.1.50:9477 and click **Start Now** or **Schedule for Tonight 10 PM**.
```

Embedded JSON (for programmatic use):

```json
{
  "status": "partial",
  "project_id": "...",
  "gpu": { "can_start": true, "util_percent": 12 },
  "instrumentation": {
    "files_created": [".blackwell/profile.json", "blackwell/runner.py"],
    "agent_tasks": [
      {"file": "train.py", "action": "add_checkpoint_hook", "line_hint": 145}
    ]
  },
  "queue_position": 2
}
```

---

## 25. Appendix C: Cursor Integration Patterns

### 25.1 Developer Workflow

1. Clone project repo
2. Open in Cursor
3. Ensure Blackwell MCP configured
4. Chat: *"Activate this project for Blackwell coordination. Owner: alice, estimated 2 hours."*
5. Cursor calls `activate` tool → agent applies instrumentation
6. Open dashboard, click Start Now or Schedule
7. Monitor GPU graph + ETA

### 25.2 Cursor Rule (auto-generated on activate)

```markdown
# Blackwell Coordinator Rules
- Never run GPU training directly. Use `blackwell-run` or the dashboard.
- Before manual runs, check GPU status at BLACKWELL_COORDINATOR_URL.
- On training script changes, preserve checkpoint/resume hooks marked BLACKWELL:START/END.
```

### 25.3 Optional: Daemon → Cursor SDK (v2)

When scheduled time arrives, daemon could call:

```typescript
await Agent.prompt(
  `Project ${projectId} is scheduled to start now. Verify GPU is idle and confirm.`,
  { local: { cwd: projectPath }, model: { id: "composer-2.5" } }
);
```

Not MVP — document for future.

---

## Summary Checklist for Implementation Agent

When implementing from this PRD, ensure:

- [ ] Exactly **one** MCP tool: `activate`
- [ ] GPU threshold **25%** with grace period before auto-start
- [ ] Gap-filling scheduler (2h before 33h night job)
- [ ] JEV for ambiguous schedule decisions; heuristics otherwise
- [ ] Local LLM only for code generation / duration inference
- [ ] Modern dark UI with live GPU line chart
- [ ] Start button greyed when GPU busy
- [ ] Per-project elapsed / remaining / finish datetime
- [ ] Locked admin logs, password obfuscated in source (not env)
- [ ] Two demo projects with full E2E tests
- [ ] Checkpoint/resume injected via activate flow
- [ ] Night preference for long jobs
- [ ] All projects onboarded **only** through MCP activate

---

*End of PRD — BlackwellCoordinator v1.0*
