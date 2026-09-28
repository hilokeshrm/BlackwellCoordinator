# BlackwellCoordinator — Plan Summary

**Date:** 2026-09-28  
**Status:** MVP built and tested (see [README](../README.md))  
**Full PRD:** [PRD-BlackwellCoordinator.md](./PRD-BlackwellCoordinator.md)

## As built (2026-09-28)

Simplified from the PRD to keep one language and no build step:

| PRD | Built |
|---|---|
| React + Vite UI | Vanilla HTML/CSS/JS served by the daemon (`coordinator/static/`), hand-drawn SVG chart |
| TypeScript MCP | Python MCP (`mcp` 2.x `MCPServer`), one tool `activate` + `/activate` prompt |
| pynvml | `nvidia-smi` polling (no extra dependency) |
| SIGTERM stop | Heartbeat `stop` action + `.blackwell/STOP` file (works on Windows), signals also handled |
| Non-goal: preemption | Added: daytime checkpoint-preemption of long jobs for short ones, auto-resume |
| Client-side password | Server-side check, hardcoded in a disguised module; logs API requires the session |
| JEV via Vercel gateway | Local Open-Jev server (m3's jev-router image, 2B/9B), Ollama and heuristics as fallback |
| Demo projects in repo | Removed; tests generate throwaway sample projects in temp dirs |

Decisions taken: GPU host is Windows (RTX PRO 6000 Blackwell, 1 GPU, 96 GB); night window 22:00–07:00 local; decisions use local Open-Jev (as in m3) with Ollama `qwen2.5:7b` then heuristics as fallback; the daemon only reads the real GPU; admin can force-start from the unlocked dashboard.

---

## What We're Building

**BlackwellCoordinator** is a three-part internal platform for a 5–6 person team sharing one Blackwell GPU system.

| Component | Purpose |
|-----------|---------|
| **Coordinator Daemon** | Background service on the GPU machine — monitors GPU, runs the queue, manages job lifecycle |
| **Web Dashboard** | Mission-control UI — GPU graph, project cards, Start/Schedule, ETAs, locked admin logs |
| **Blackwell MCP** | Single Cursor entry point: **`activate`** — instruments projects and registers them with the coordinator |

---

## Core Behaviors

1. **GPU < 25% → auto-start queue** — with a 60s grace period so brief dips don't trigger false starts
2. **Gap-filling scheduling** — a 2h finetune runs this afternoon; a 33h training waits for tonight (22:00–07:00)
3. **JEV for decisions, heuristics for everything else** — hard rules and timeline math first; JEV only for tie-breaks; local LLM only for code generation during `activate` and duration inference
4. **MCP `activate` only** — one tool that scans the project, tells Cursor what to patch (checkpoints, resume, logging, milestones), registers with the daemon, and reports GPU capacity
5. **Start greyed out** when GPU ≥ 25% or another job is running
6. **Live GPU line chart** — WebSocket, ~2s updates
7. **Per-project time info** — elapsed, remaining, projected finish date/time
8. **Locked admin logs** — password obfuscated in source (not `.env`)
9. **Night preference** — long jobs (>8h) default to the night window when the machine is usually free

---

## Architecture (High Level)

```
Cursor (each teammate) ──MCP activate()──► Blackwell MCP
                                                │
                                                ▼
                                         Coordinator Daemon ◄──► SQLite
                                                │
                    ┌───────────────────────────┼───────────────────────────┐
                    ▼                           ▼                           ▼
              Web Dashboard              Job subprocesses            JEV / Ollama
              (GPU graph, queue)         (blackwell-run wrapper)     (scheduling)
```

Projects are onboarded **only** via MCP `activate` — no manual registration.

---

## Implementation Phases (~3 weeks)

| Phase | Days | Deliverable |
|-------|------|-------------|
| 0 — Scaffold | 1 | Monorepo, shared types, README |
| 1 — Daemon | 2–4 | GPU polling, REST/WS API, job runner |
| 2 — Scheduler | 4–6 | Gap-filling + JEV integration |
| 3 — MCP | 6–9 | `activate` tool + project scanner |
| 4 — Instrumentation | 8–10 | `blackwell-run` package, signal handlers |
| 5 — UI | 10–14 | Dashboard, graph, cards, locked logs |
| 6 — Demo E2E | 14–16 | Two demo projects, full test matrix |
| 7 — Polish | 16–18 | Windows service, docs, QA |

---

## Demo Projects (Planned)

1. **`demo-quick-finetune`** — ~10 min real time (simulates 2h); tests gap-filling and checkpoint resume
2. **`demo-long-training`** — ~30 min real time (simulates 33h); tests night scheduling and multi-day ETAs

Both use a `--mock-gpu` mode so CI runs without NVIDIA hardware.

---

## Extras Added Beyond Original Request

- Gantt-style timeline view of the queue
- Job state machine: `queued → running → checkpointing → paused → completed`
- Heartbeat every 30s for live progress
- Graceful SIGTERM → checkpoint → resume (no more full restarts)
- Stale process detection (no heartbeat for 5 min)
- Optional Cursor SDK bridge (v2) for daemon-triggered agent prompts
- Slack/webhook notifications (post-MVP)
- 12-point E2E test matrix in the full PRD

---

## Decisions Needed Before Build

1. **GPU host OS** — Windows or Linux on the Blackwell machine?
2. **GPU count** — 1 or multiple?
3. **Night window** — Is 22:00–07:00 IST correct?
4. **JEV API key** — Do you have Vercel AI Gateway / TypeSafe access? (Heuristic fallback works without it.)
5. **Local LLM** — Which Ollama models are installed for duration inference?
6. **Team lead override** — Should you be able to force-start when GPU is busy (behind admin lock)?

---

## Documents in This Repo

| File | Description |
|------|-------------|
| [PLAN-SUMMARY.md](./PLAN-SUMMARY.md) | This document — high-level plan overview |
| [PRD-BlackwellCoordinator.md](./PRD-BlackwellCoordinator.md) | Full product requirements — API specs, data models, UI wireframes, MCP schemas, scheduling algorithms, test matrix, repo structure |

---

## Next Step

When ready to build, start with **Phase 0 (scaffold) + Phase 1 (daemon core)** and the two demo projects. Answering the six decisions above first will let implementation proceed without blocking.
