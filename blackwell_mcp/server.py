"""Blackwell MCP — one tool, `activate`, that onboards a project onto the shared Blackwell GPU coordinator.

Run it again at any time: it re-scans, verifies the instrumentation the Cursor agent applied, refreshes the
registration, and reports GPU/queue state plus any failure from the project's last run.
"""
import json
import os
import py_compile
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import logging

import httpx
from mcp.server.mcpserver import MCPServer

from . import scanner

logging.getLogger("httpx").setLevel(logging.WARNING)
COORD = os.environ.get("BLACKWELL_COORDINATOR_URL", "http://127.0.0.1:9477").rstrip("/")
TOKEN = os.environ.get("BLACKWELL_API_TOKEN", "")
OLLAMA = os.environ.get("BLACKWELL_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("BLACKWELL_OLLAMA_MODEL", "qwen2.5:7b")
TEMPLATES = Path(__file__).parent / "templates"
VERSION = "1.0.0"

mcp = MCPServer(
    "blackwell-coordinator",
    instructions=(
        "Blackwell GPU coordinator. The only tool is `activate`: call it with the workspace root to onboard or "
        "re-check a project, then carry out every 'Agent task' in its report exactly, then call `activate` again "
        "to verify. Never launch GPU training directly; runs go through the coordinator dashboard."
    ),
)

RULE = """---
description: BlackwellCoordinator — shared GPU rules for this project
alwaysApply: true
---
# Blackwell GPU coordination
- This project runs on the team's shared Blackwell GPU through BlackwellCoordinator ({url}).
- Never launch training/finetuning/benchmarks directly on the GPU. Use the dashboard: **Start Now** or **Schedule**.
- Keep the code between `# BLACKWELL:START` and `# BLACKWELL:END` markers intact: it provides checkpoint/resume,
  progress heartbeats and graceful stop (the coordinator can pause a job so a short job fits, then resume it).
- If you change the training loop, keep calling `bw.progress(...)`, `bw.checkpoint(...)` and honour `bw.should_stop`.
- After changing the entry command, epochs, or expected duration, call the `activate` MCP tool again.
- Project ID: `{project_id}` · Owner: {owner}
"""


def _client():
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    return httpx.Client(base_url=COORD + "/api/v1", timeout=10, headers=headers)


def _estimate_with_llm(res: scanner.ScanResult) -> tuple[float | None, str]:
    entry = _safe_read(res.root / res.entry_file)[:4000] if res.entry_file else ""
    prompt = (
        "Estimate wall-clock minutes for this ML job on one NVIDIA RTX PRO 6000 Blackwell (96 GB). "
        'Reply JSON only: {"minutes": <number>, "why": "<10 words>"}\n\n'
        f"README:\n{res.readme[:1500]}\n\nENTRY ({res.entry_file}):\n{entry}\n\n"
        f"epochs={res.total_epochs} steps={res.total_steps} framework={res.framework}"
    )
    try:
        r = httpx.post(f"{OLLAMA}/api/generate", timeout=45, json={
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json", "options": {"temperature": 0}})
        d = json.loads(r.json()["response"])
        m = float(d["minutes"])
        if 1 <= m <= 60 * 24 * 14:
            return m, f"local LLM ({OLLAMA_MODEL}): {d.get('why', '')}"
    except Exception:
        pass
    return None, ""


def _safe_read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _fmt_min(m: float | None) -> str:
    if m is None:
        return "?"
    m = float(m)
    if m < 60:
        return f"{m:.0f} min"
    h, mm = divmod(round(m), 60)
    return f"{h}h {mm:02d}m" if h < 48 else f"{h // 24}d {h % 24}h"


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b, %I:%M %p") if ts else "—"


def _last_run_tail(root: Path) -> tuple[str | None, str]:
    logs = sorted((root / ".blackwell" / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None, ""
    lines = _safe_read(logs[-1]).splitlines()
    return logs[-1].name, "\n".join(lines[-30:])


def _gitignore(root: Path):
    gi = root / ".gitignore"
    want = [".blackwell/checkpoints/", ".blackwell/logs/", ".blackwell/STOP", ".blackwell/*.tmp"]
    have = _safe_read(gi)
    missing = [w for w in want if w not in have]
    if missing:
        with open(gi, "a", encoding="utf-8") as fh:
            fh.write(("\n" if have and not have.endswith("\n") else "") + "# BlackwellCoordinator\n" + "\n".join(missing) + "\n")


def _agent_tasks(res: scanner.ScanResult) -> list[str]:
    if res.sdk_integrated:
        return []
    f = res.entry_file or "<your training script>"
    total = res.total_epochs or res.total_steps or "<total epochs or steps>"
    unit = "epoch" if res.total_epochs or not res.total_steps else "step"
    common = (
        f"Wrap every inserted block in `# BLACKWELL:START` / `# BLACKWELL:END` comments. Do not change model/"
        f"training maths. `blackwell_sdk.py` (already copied to the project root) is stdlib-only."
    )
    if res.framework == "huggingface":
        return [
            f"In `{f}`, add `from blackwell_sdk import Blackwell` and create `bw = Blackwell(total_steps=<trainer max_steps or "
            f"epochs*steps_per_epoch>)` before the Trainer is built.",
            "Add a `transformers.TrainerCallback` subclass: in `on_log` call `bw.progress(state.global_step, metrics=logs)`; in "
            "`on_step_end`, if `bw.should_stop`: set `control.should_save = True` and `control.should_training_stop = True`. "
            "Register it with `trainer.add_callback(...)`.",
            "Set `save_strategy='steps'`, a sensible `save_steps`, and `save_total_limit=3` in TrainingArguments so resumes lose little work.",
            "Resume: `trainer.train(resume_from_checkpoint=transformers.trainer_utils.get_last_checkpoint(args.output_dir))` "
            "(pass None when there is none). After a stop-triggered exit call `bw.exit_paused()`; after normal finish call `bw.complete()`.",
            common,
        ]
    if res.framework == "lightning":
        return [
            f"In `{f}`, add `from blackwell_sdk import Blackwell`; `bw = Blackwell(total_steps=trainer.max_epochs)`.",
            "Add a `lightning.Callback`: `on_train_epoch_end` → `bw.progress(trainer.current_epoch + 1, metrics={k: float(v) for k, v in "
            "trainer.callback_metrics.items()})`; if `bw.should_stop`: `trainer.save_checkpoint(bw.ckpt_dir / 'last.ckpt')` then "
            "`trainer.should_stop = True`.",
            "Add `ModelCheckpoint(dirpath='.blackwell/checkpoints', save_last=True)` and call "
            "`trainer.fit(model, ckpt_path='last' if (bw.ckpt_dir / 'last.ckpt').exists() else None)`.",
            "After fit: `bw.exit_paused()` if `bw.should_stop` else `bw.complete()`.",
            common,
        ]
    if f.endswith(".sh"):
        return [
            f"`{f}` is a shell script: move the training loop into a Python entry (or make the Python script it calls use the SDK) "
            "so checkpoints/resume work, then call `activate` again with `entry_command` pointing at it.",
        ]
    save = ("torch.save({'model': model.state_dict(), 'optim': optimizer.state_dict(), 'step': step}, path)"
            if res.framework == "pytorch" else "write your training state (weights, optimizer, RNG) to `path`")
    load = ("state = torch.load(ckpt.path); model.load_state_dict(state['model']); optimizer.load_state_dict(state['optim'])"
            if res.framework == "pytorch" else "load that state back from `ckpt.path` (or use `bw.load_json()` if you saved with `bw.checkpoint_json`)")
    return [
        f"In `{f}`, add `from blackwell_sdk import Blackwell` and, before the training loop, `bw = Blackwell(total_steps={total})`.",
        f"Resume: `ckpt = bw.latest_checkpoint()`; if it exists, {load} and start the loop at `int(ckpt.step)` instead of 0. "
        f"For small pure-Python state, `bw.checkpoint_json(step, state_dict)` / `bw.load_json()` is enough.",
        f"At the end of each {unit} (with the 1-based {unit} number `n`): `bw.progress(n, metrics={{'loss': loss}})`.",
        f"Checkpoint: `if bw.checkpoint_due(n, every=<pick so a checkpoint lands every ~5-15 min>) or bw.should_stop: "
        f"bw.checkpoint(n, save_fn)` where `save_fn(path)` does: {save}.",
        "Graceful stop: right after checkpointing, `if bw.should_stop: bw.exit_paused()`. After the loop finishes and outputs "
        "are saved: `bw.complete()`.",
        "Optional: `bw.milestone('name')` for notable events (eval done, best loss, etc.).",
        common,
    ]


def _report(res, profile, reg, tasks, compiled_ok, compile_err, est_src, created, tail_name, tail, err=None) -> str:
    status = "failed" if err else ("complete" if not tasks and compiled_ok else "partial")
    L = [f"## Blackwell Activation Report — `{profile['name']}`", "",
         f"**Status:** {status}  ·  **Project ID:** `{profile['project_id']}`  ·  **Owner:** {profile['owner']}",
         f"**Coordinator:** {COORD}  ·  **Dashboard:** {COORD}/", ""]
    if err:
        L += [f"> ❌ {err}", ""]
    L += ["### Project", f"- Entry command: `{profile['entry_command']}`", f"- Framework: {res.framework}",
          f"- Estimated duration: **{_fmt_min(profile['estimated_duration_minutes'])}** ({est_src})",
          f"- Expected GPU load: ~{profile['estimated_gpu_utilization_percent']:.0f}%"
          + (f", ~{profile['estimated_vram_gb']:.0f} GB VRAM" if profile.get("estimated_vram_gb") else " (VRAM learned after the first run)"),
          f"- Priority: {profile['priority']} · Window: {profile['preferred_window']}",
          f"- Resume support: {'✅ verified (SDK wired in)' if profile['supports_resume'] else '⚠️ not yet — see agent tasks'}", ""]
    if reg:
        p = reg["project"]
        g = reg.get("gpu") or {}
        L += ["### GPU", f"- {g.get('name', 'GPU')}: **{g.get('utilization_percent', 0):.0f}%** util, "
              f"{g.get('memory_used_mb', 0) / 1024:.1f}/{g.get('memory_total_mb', 0) / 1024:.0f} GB VRAM"
              + (" (simulated)" if reg.get("mock_gpu") else ""),
              f"- Start Now: {'**allowed**' if p['can_start'] else '**greyed out** — ' + str(p['blocked_reason'])}",
              f"- If this starts: ~{g.get('utilization_percent', 0):.0f}% → ~{min(100, g.get('utilization_percent', 0) + p['est_gpu_percent']):.0f}%", ""]
        L += ["### Queue", f"- This project: **{p['status']}**"
              + (f" · {p['percent']:.1f}% done · ~{_fmt_min(p['remaining_min'])} left" if p["percent"] else "")]
        if p.get("projected_finish"):
            L.append(f"- Projected: start {_fmt_ts(p['projected_start'])} → finish **{_fmt_ts(p['projected_finish'])}** ({p['plan_reason']})")
        others = [t for t in reg["timeline"] if t["project_id"] != p["id"]]
        for t in others[:6]:
            L.append(f"- {t['name']} ({t['owner']}): {_fmt_ts(t['start'])} → {_fmt_ts(t['end'])} [{t['status']}]")
        if not others:
            L.append("- Nothing else queued.")
        L.append("")
    L += ["### Instrumentation", *[f"- [x] {c}" for c in created],
          f"- [{'x' if res.sdk_integrated else ' '}] Training script uses `blackwell_sdk` (progress, checkpoints, graceful stop)",
          f"- [{'x' if compiled_ok else ' '}] Entry script compiles" + (f" — `{compile_err}`" if compile_err else ""), ""]
    if tasks:
        L += ["### Agent tasks (Cursor: do these now, then call `activate` again to verify)", *[f"{i}. {t}" for i, t in enumerate(tasks, 1)], ""]
    if tail_name and reg and reg["project"]["status"] == "failed":
        L += [f"### ⚠️ Last run failed (`.blackwell/logs/{tail_name}`)", "Fix the error below, then re-activate:", "```", tail, "```", ""]
    L += ["### Next step"]
    if tasks:
        L.append("Apply the agent tasks above, then call `activate` again.")
    elif reg and reg["project"]["can_start"]:
        L.append(f"Open {COORD}/ → **Start Now**, or **Schedule** (tonight {reg['night']['start_hour']}:00 is the quiet window).")
    else:
        L.append(f"Open {COORD}/ → **Schedule** (auto: the coordinator starts it when the GPU is idle below the threshold).")
    return "\n".join(L)


@mcp.tool()
def activate(
    project_path: str,
    owner: str,
    estimated_duration_minutes: float | None = None,
    priority: str = "normal",
    preferred_window: str = "anytime",
    entry_command: str | None = None,
    expected_gpu_percent: float | None = None,
    expected_vram_gb: float | None = None,
    kind: str = "auto",
    health_url: str | None = None,
    process_match: list[str] | None = None,
    name: str | None = None,
) -> str:
    """Onboard (or re-check) a project on the shared Blackwell GPU.

    Training jobs (finetuning, training, benchmarks): scans the project, installs the Blackwell SDK
    (checkpoint/resume, milestones, heartbeats, graceful stop), writes .blackwell/profile.json and a Cursor rule,
    registers it in the coordinator queue, and reports GPU availability and ETA. Returns agent tasks the Cursor
    agent must apply to the training script; call activate again afterwards to verify.

    Deployed services (always-on apps such as an agent platform or an inference server): registers the service so
    the dashboard health-checks it, shows its GPU memory, and keeps VRAM free for it if it goes down. No code changes.

    Args:
        kind: auto | job | service. auto treats a folder with docker compose files and no training script as a service.
        health_url: Services only. URL that answers when the service is up (auto-detected from the README).
        process_match: Services only. Process name/command-line fragments that belong to it (e.g. ["llama-server"]).
        name: Display name on the dashboard (defaults to the folder name).
        project_path: Absolute path of the project root (the Cursor workspace folder).
        owner: Teammate name/handle who owns the project.
        estimated_duration_minutes: Expected run time. If omitted: previous value, else a local-LLM estimate.
        priority: low | normal | high | critical.
        preferred_window: anytime | night_only | daytime_only. Jobs > 8h prefer the night anyway.
        entry_command: Command that runs the job (e.g. "python train.py --config cfg.yaml"). Auto-detected if omitted.
        expected_gpu_percent: Expected GPU utilisation while running (default 90, learned from real runs).
        expected_vram_gb: Expected peak VRAM in GB (learned from real runs; Start is blocked if it will not fit).
    """
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        return f"❌ project_path does not exist: {root}"
    priority = priority if priority in ("low", "normal", "high", "critical") else "normal"
    preferred_window = preferred_window if preferred_window in ("anytime", "night_only", "daytime_only") else "anytime"
    bw = root / ".blackwell"
    bw.mkdir(exist_ok=True)
    prev = scanner.read_json(bw / "profile.json")

    # 1. Preflight
    health_err = None
    try:
        with _client() as c:
            c.get("/health").raise_for_status()
    except Exception as e:
        health_err = f"Coordinator not reachable at {COORD} ({type(e).__name__}). Start it on the GPU host: `scripts/start-coordinator.ps1`."

    kind = kind if kind in ("job", "service") else (prev.get("kind") or None)
    if kind is None:
        kind = "service" if scanner.service_hints(root)["looks_like_service"] and not entry_command else "job"
    if kind == "service":
        return _activate_service(root, owner, prev, health_err, health_url, process_match, expected_vram_gb, name)

    # 2. Discovery
    res = scanner.scan(root, entry_command or prev.get("entry_command"))
    if not res.entry_command:
        return "❌ No training entry point found. Call activate again with `entry_command`, e.g. \"python train.py\"."

    # 3. Estimates
    est, est_src = estimated_duration_minutes, "given"
    if est is None and prev.get("estimated_duration_minutes") and prev.get("entry_command") == res.entry_command:
        est, est_src = prev["estimated_duration_minutes"], "previous activation"
    if est is None:
        est, est_src = _estimate_with_llm(res)
    if est is None:
        est, est_src = 60.0, "default (pass estimated_duration_minutes for a better plan)"
    gpu_pct = expected_gpu_percent or prev.get("estimated_gpu_utilization_percent") or 90.0
    vram_gb = expected_vram_gb if expected_vram_gb is not None else prev.get("estimated_vram_gb")
    explicit = {**({"est_gpu_percent": expected_gpu_percent} if expected_gpu_percent else {}),
                **({"est_vram_mb": expected_vram_gb * 1024} if expected_vram_gb else {})}

    # 4. Instrumentation files (the training-loop patch itself is done by the Cursor agent)
    created = []
    sdk_dst = root / "blackwell_sdk.py"
    if not sdk_dst.exists() or "SDK_VERSION" not in _safe_read(sdk_dst) or \
            re.search(r'SDK_VERSION = "([^"]+)"', _safe_read(sdk_dst)).group(1) != VERSION:
        shutil.copy2(TEMPLATES / "blackwell_sdk.py", sdk_dst)
        created.append("Installed `blackwell_sdk.py` (checkpoint/resume, milestones, heartbeats, graceful stop)")
    else:
        created.append("`blackwell_sdk.py` up to date")
    (bw / "checkpoints").mkdir(exist_ok=True)
    (bw / "logs").mkdir(exist_ok=True)
    _gitignore(root)

    compiled_ok, compile_err = True, None
    if res.entry_file and res.entry_file.endswith(".py") and (root / res.entry_file).exists():
        try:
            py_compile.compile(str(root / res.entry_file), doraise=True, cfile=str(bw / "compile-check.pyc"))
        except py_compile.PyCompileError as e:
            compiled_ok, compile_err = False, str(e.msg).strip().splitlines()[-1][:200]
        (bw / "compile-check.pyc").unlink(missing_ok=True)

    profile = {
        "project_id": prev.get("project_id"), "name": name or prev.get("name") or res.name, "owner": owner, "kind": "job",
        "framework": res.framework, "entry_command": res.entry_command, "entry_file": res.entry_file,
        "estimated_duration_minutes": est, "estimated_gpu_utilization_percent": gpu_pct, "estimated_vram_gb": vram_gb,
        "priority": priority, "preferred_window": preferred_window,
        "supports_resume": res.sdk_integrated and compiled_ok, "total_epochs": res.total_epochs,
        "total_steps": res.total_steps, "config_file": res.config_file, "coordinator_url": COORD,
        "activated_at": prev.get("activated_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_activate": datetime.now().astimezone().isoformat(timespec="seconds"),
        "coordinator_version": VERSION,
    }

    # 5. Registration
    reg, err = None, health_err
    if not health_err:
        try:
            with _client() as c:
                r = c.post("/projects", json={
                    "name": profile["name"], "path": str(root), "owner": owner, "entry_command": res.entry_command, "kind": "job",
                    "framework": res.framework, "estimated_duration_min": est, **explicit,
                    "priority": priority, "preferred_window": preferred_window,
                    "supports_resume": profile["supports_resume"]})
                r.raise_for_status()
                reg = r.json()
                profile["project_id"] = reg["project"]["id"]
                profile["estimated_gpu_utilization_percent"] = reg["project"]["est_gpu_percent"]
                if reg["project"].get("est_vram_mb"):
                    profile["estimated_vram_gb"] = round(reg["project"]["est_vram_mb"] / 1024, 1)
        except httpx.HTTPStatusError as e:
            err = f"Coordinator rejected registration: {e.response.status_code} {e.response.text[:200]}"
        except Exception as e:
            err = f"Registration failed: {e}"

    profile["project_id"] = profile["project_id"] or "unregistered"
    (bw / "profile.json").write_text(json.dumps(profile, indent=2))
    created.append("Wrote `.blackwell/profile.json`")
    rules = root / ".cursor" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "blackwell.mdc").write_text(RULE.format(url=COORD, project_id=profile["project_id"], owner=owner))
    created.append("Wrote Cursor rule `.cursor/rules/blackwell.mdc`")
    created.append("Updated `.gitignore` (checkpoints/logs ignored)")

    tasks = _agent_tasks(res)
    if not compiled_ok:
        tasks.insert(0, f"`{res.entry_file}` does not compile: {compile_err}. Fix it first.")
    tail_name, tail = _last_run_tail(root)
    report = _report(res, profile, reg, tasks, compiled_ok, compile_err, est_src, created, tail_name, tail, err)
    payload = {"status": "failed" if err else ("complete" if not tasks else "partial"),
               "project_id": profile["project_id"], "supports_resume": profile["supports_resume"],
               "gpu": {"can_start": bool(reg and reg["project"]["can_start"]),
                       "util_percent": (reg or {}).get("gpu", {}).get("utilization_percent")},
               "scan": scanner.as_dict(res), "agent_tasks": tasks, "ts": time.time()}
    return report + "\n\n```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"


SERVICE_RULE = """---
description: BlackwellCoordinator — this project is a deployed service on the shared GPU
alwaysApply: true
---
# Blackwell GPU coordination (deployed service)
- This project is registered with BlackwellCoordinator ({url}) as an always-on **deployed service**.
- The coordinator health-checks it ({health}) and keeps its GPU memory (~{vram}) free for it when it is down.
- Training, finetuning or benchmark runs launched from here must not go straight to the GPU: put them in their own
  folder and call the `activate` MCP tool there so they are queued as jobs.
- If the service's ports, health URL or model sizes change, call `activate` again in this folder.
- Project ID: `{project_id}` · Owner: {owner}
"""


def _activate_service(root: Path, owner: str, prev: dict, health_err, health_url, process_match, vram_gb, name=None) -> str:
    hints = scanner.service_hints(root)
    health = health_url or prev.get("health_url") or hints["health_url"]
    match = process_match if process_match is not None else (
        prev.get("process_match") or (["llama-server", "ollama"] if hints["uses_ollama"] else []))
    vram = vram_gb if vram_gb is not None else prev.get("estimated_vram_gb")
    name = name or prev.get("name") or root.name
    profile = {"project_id": prev.get("project_id"), "name": name, "owner": owner, "kind": "service",
               "health_url": health, "process_match": match, "estimated_vram_gb": vram, "coordinator_url": COORD,
               "compose_files": hints["compose_files"],
               "activated_at": prev.get("activated_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
               "last_activate": datetime.now().astimezone().isoformat(timespec="seconds"), "coordinator_version": VERSION}
    reg, err = None, health_err
    if not health_err:
        try:
            with _client() as c:
                r = c.post("/projects", json={
                    "name": name, "path": str(root), "owner": owner, "kind": "service", "health_url": health,
                    "process_match": match, "estimated_duration_min": 1, "supports_resume": False,
                    **({"est_vram_mb": vram * 1024} if vram else {})})
                r.raise_for_status()
                reg = r.json()
                profile["project_id"] = reg["project"]["id"]
        except httpx.HTTPStatusError as e:
            err = f"Coordinator rejected registration: {e.response.status_code} {e.response.text[:200]}"
        except Exception as e:
            err = f"Registration failed: {e}"
    profile["project_id"] = profile["project_id"] or "unregistered"
    bw = root / ".blackwell"
    (bw / "profile.json").write_text(json.dumps(profile, indent=2))
    svc = reg["project"] if reg else {}
    vram_txt = f"{svc['vram_mb'] / 1024:.1f} GB" if svc.get("vram_mb") else (f"{vram:.0f} GB" if vram else "learned while running")
    rules = root / ".cursor" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "blackwell.mdc").write_text(SERVICE_RULE.format(url=COORD, health=health or "process check",
                                                              vram=vram_txt, project_id=profile["project_id"], owner=owner))
    state = svc.get("svc_state", "unknown")
    L = [f"## Blackwell Activation Report — `{name}` (deployed service)", "",
         f"**Status:** {'failed' if err else 'complete'}  ·  **Project ID:** `{profile['project_id']}`  ·  **Owner:** {owner}",
         f"**Dashboard:** {COORD}/", ""]
    if err:
        L += [f"> ❌ {err}", ""]
    L += ["### Service",
          f"- Health check: `{health or 'none'}` → **{state.upper()}**" + (f" ({svc.get('svc_detail')})" if svc.get("svc_detail") else ""),
          f"- GPU processes matched: {', '.join(f'`{m}`' for m in match) or 'none (pass process_match to track its VRAM)'}",
          f"- GPU memory now: {vram_txt}",
          f"- Compose files: {', '.join(hints['compose_files']) or 'none found'}", "",
          "### What the coordinator does with it",
          "- Shows it under **Deployed services** with up/down status and its GPU memory.",
          "- Its GPU load does not count against the auto-start threshold; its VRAM does.",
          "- While it is down, its VRAM stays reserved so a training job can't take it.",
          "- It is never queued, started or stopped by the coordinator.", "",
          "### Files", "- Wrote `.blackwell/profile.json`", "- Wrote Cursor rule `.cursor/rules/blackwell.mdc`", "",
          "No code changes are needed for a service."]
    payload = {"status": "failed" if err else "complete", "kind": "service", "project_id": profile["project_id"],
               "health_url": health, "state": state, "process_match": match, "hints": hints, "ts": time.time()}
    return "\n".join(L) + "\n\n```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"


@mcp.prompt(name="activate", description="Onboard this workspace onto the shared Blackwell GPU coordinator")
def activate_prompt(owner: str = "", duration_hint: str = "", window: str = "") -> str:
    return (
        "Onboard this project onto the team's shared Blackwell GPU.\n"
        f"1. Call the `activate` MCP tool with project_path = the absolute path of this workspace root"
        f"{', owner = ' + repr(owner) if owner else ', owner = my name (ask me if unknown)'}"
        f"{', estimated_duration_minutes from: ' + duration_hint if duration_hint else ''}"
        f"{', preferred_window = ' + repr(window) if window else ''}. "
        "If this folder is a deployed, always-on app (an agent platform, an API, an inference server) rather than "
        "training/finetuning/benchmark code, pass kind = 'service' and stop after step 1.\n"
        "2. Apply every item under 'Agent tasks' in the report exactly (checkpoint/resume, progress, graceful stop), "
        "keeping the BLACKWELL:START/END markers.\n"
        "3. Call `activate` again with the same arguments and confirm Status is `complete`.\n"
        "4. Tell me the GPU status, ETA/projected finish, and whether Start Now is available on the dashboard."
    )


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
