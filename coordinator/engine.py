import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx
import psutil

from . import decider, scheduler
from .config import Settings
from .db import DB
from .gpu import MockGpuProvider, make_provider
from .gpuproc import ProcSampler, descendants

ACTIVE = ("running", "stopping")
VM_PROCS = {"vmwp.exe", "vmmem", "vmmemwsl", "vmmem.exe"}
DESKTOP_PROCS = {"dwm.exe", "explorer.exe", "csrss.exe", "chrome.exe", "msedge.exe", "msedgewebview2.exe", "firefox.exe",
                 "cursor.exe", "code.exe", "teams.exe", "ms-teams.exe", "slack.exe", "discord.exe", "searchhost.exe",
                 "shellexperiencehost.exe", "startmenuexperiencehost.exe", "textinputhost.exe", "applicationframehost.exe",
                 "teamviewer.exe", "docker desktop.exe", "windowsterminal.exe", "shellhost.exe", "widgets.exe",
                 "phoneexperiencehost.exe", "crossdeviceresume.exe", "logioptionsplus_agent.exe", "promecefpluginhost.exe",
                 "wps.exe", "obs64.exe", "systemsettings.exe"}
LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|Traceback)\b")


class Engine:
    def __init__(self, s: Settings):
        self.s = s
        self.db = DB(s.db_path)
        self.gpu = make_provider(s.mock_gpu)
        self.mock = isinstance(self.gpu, MockGpuProvider)
        self.latest: list[dict] = []
        self.procs: dict[str, subprocess.Popen] = {}
        self.run_gpu: dict[str, list[float]] = {}
        self.run_mem: dict[str, list[float]] = {}  # [baseline_mb, peak_mb]
        self.events: deque = deque(maxlen=50)
        self.idle_since: float | None = None
        self.last_decision: dict | None = None
        self.jev = decider.JevClient(s)
        self.preempt_verdicts: dict[tuple, tuple[float, bool]] = {}
        self.sampler = None if s.mock_gpu else ProcSampler()
        self.gpu_procs: list[dict] = []
        self.service_util = 0.0
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._last_account = time.time()
        self._recover()

    # ---------- lifecycle ----------
    def start(self):
        self.sample_gpu()
        if self.s.decider in ("auto", "jev"):
            threading.Thread(target=self.jev.warm, daemon=True).start()
            threading.Thread(target=self._loop, args=(300, self._jev_probe), daemon=True).start()
        threading.Thread(target=self._loop, args=(self.s.gpu_poll_seconds, self.sample_gpu), daemon=True).start()
        threading.Thread(target=self._loop, args=(self.s.scheduler_tick_seconds, self.tick), daemon=True).start()
        threading.Thread(target=self._loop, args=(self.s.service_check_seconds, self.check_services), daemon=True).start()
        threading.Thread(target=self.check_services, daemon=True).start()
        threading.Thread(target=self._loop, args=(3600, lambda: self.db.prune(self.s.log_retention_days)), daemon=True).start()

    def shutdown(self):
        self._stop.set()

    def _loop(self, every, fn):
        while not self._stop.wait(every):
            try:
                fn()
            except Exception as e:  # keep loops alive
                self.event(f"internal error in {fn.__name__}: {e}", "ERROR")

    def _jev_probe(self):
        if not self.jev.online:
            self.jev.warm()

    def jobs(self) -> list[dict]:
        return [p for p in self.db.projects() if p["kind"] == "job"]

    def services(self) -> list[dict]:
        return [p for p in self.db.projects() if p["kind"] == "service"]

    def _recover(self):
        for p in self.jobs():
            if p["status"] in ACTIVE:
                # PIDs may have been reused, so never kill by PID here; ask the orphan to checkpoint and exit
                try:
                    (Path(p["path"]) / ".blackwell" / "STOP").write_text(str(time.time()))
                except OSError:
                    pass
                nxt = "queued" if p["supports_resume"] else "failed"
                self.db.update_project(p["id"], status=nxt, pid=None, current_run_id=None, stop_requested=0,
                                       message="Coordinator restarted — " + ("will resume from checkpoint" if nxt == "queued" else "run lost"))
                self.db.log(p["id"], f"Recovered after coordinator restart → {nxt}", "WARNING")

    def event(self, msg, level="INFO", project_id=None):
        self.events.appendleft({"ts": time.time(), "level": level, "message": msg, "project_id": project_id})
        self.db.log(project_id, msg, level, source="scheduler")

    # ---------- GPU ----------
    def sample_gpu(self):
        if self.mock:
            self.gpu.job_load = sum(p["est_gpu_percent"] for p in self.db.projects() if p["status"] in ACTIVE)
        self.latest = [g.as_dict() for g in self.gpu.sample()]
        g = self.latest[0]
        self.db.add_gpu_sample(g["ts"], g["utilization_percent"], g["memory_used_mb"], g["memory_total_mb"])
        now = time.time()
        running = [p for p in self.jobs() if p["status"] in ACTIVE]
        self._attribute(running)
        if self.util() < self.s.idle_threshold_percent and not running:
            self.idle_since = self.idle_since or now
        else:
            self.idle_since = None
        dt = now - self._last_account
        self._last_account = now
        for p in running:
            self.db.update_project(p["id"], active_seconds=(p["active_seconds"] or 0) + dt)
            self.run_gpu.setdefault(p["id"], []).append(g["utilization_percent"])
            if p["id"] in self.run_mem:
                self.run_mem[p["id"]][1] = max(self.run_mem[p["id"]][1], g["memory_used_mb"])
        self._reap()

    def raw_util(self) -> float:
        return self.latest[0]["utilization_percent"] if self.latest else 0.0

    def util(self) -> float:
        """Utilisation the idle threshold looks at: deployed services' own load is excluded (they are always on)."""
        u = self.raw_util()
        if self.s.exclude_service_load:
            u = max(0.0, u - self.service_util)
        return u

    def _attribute(self, running: list[dict]):
        """Label every process holding GPU memory: coordinator job, deployed service, WSL/Docker VM, desktop, untracked."""
        if not self.sampler:
            self.gpu_procs, self.service_util = [], 0.0
            return
        procs = self.sampler.sample()
        job_pids = {}
        for p in running:
            proc = self.procs.get(p["id"])
            if proc:
                for pid in descendants(proc.pid):
                    job_pids[pid] = p
        services = self.services()
        for r in procs:
            hay = f"{r['name']} {r['cmd']}".lower()
            svc = next((s for s in services if any(m.lower() in hay for m in s["process_match"] if m)), None)
            if r["pid"] in job_pids:
                j = job_pids[r["pid"]]
                r.update(group="job", label=j["name"], owner=j["owner"], project_id=j["id"])
            elif svc:
                r.update(group="service", label=svc["name"], owner=svc["owner"], project_id=svc["id"])
            elif r["name"].lower() in VM_PROCS:
                r.update(group="vm", label="WSL / Docker VM")
            elif r["name"].lower() in DESKTOP_PROCS or (r["mem_mb"] < 600 and r["util"] < 5):
                r.update(group="desktop", label="Desktop & apps")
            else:
                r.update(group="untracked", label=r["name"])
        self.gpu_procs = procs
        self.service_util = min(100.0, sum(r["util"] for r in procs if r["group"] == "service"))

    def reserved_vram(self) -> float:
        """VRAM held back for deployed services that are down, so a training job can't take their memory."""
        return sum(s["est_vram_mb"] or 0 for s in self.services() if s["svc_state"] == "down")

    def busiest(self) -> str:
        top = max((r for r in self.gpu_procs if r.get("group") != "service"), key=lambda r: r["util"], default=None)
        if not top or top["util"] < 5:
            return ""
        return f" — mostly {top['label']}" + (f" ({top['name']})" if top["label"] != top["name"] else "")

    # ---------- deployed services ----------
    def check_services(self):
        for svc in self.services():
            state, detail = self._probe(svc)
            mem = sum(r["mem_mb"] for r in self.gpu_procs if r.get("project_id") == svc["id"])
            fields = {"svc_checked": time.time(), "svc_detail": detail}
            if state == "up" and mem > (svc["est_vram_mb"] or 0):
                fields["est_vram_mb"] = round(mem)
            if state != svc["svc_state"]:
                fields.update(svc_state=state, svc_since=time.time())
                if svc["svc_state"] != "unknown" or state == "down":
                    self.event(f"Service '{svc['name']}' is {'back up' if state == 'up' else 'DOWN'} ({detail})",
                               "INFO" if state == "up" else "WARNING", svc["id"])
            self.db.update_project(svc["id"], **fields)

    def _probe(self, svc) -> tuple[str, str]:
        if svc.get("health_url"):
            try:
                t = time.time()
                r = httpx.get(svc["health_url"], timeout=4, verify=False, follow_redirects=True)
                return "up", f"HTTP {r.status_code} in {(time.time() - t) * 1000:.0f} ms"
            except Exception as e:
                reason = "connection refused" if "10061" in str(e) or "refused" in str(e).lower() else type(e).__name__
                return "down", f"not answering ({reason})"
        if svc["process_match"]:
            for pr in psutil.process_iter(["name", "cmdline"]):
                try:
                    hay = f"{pr.info['name']} {' '.join(pr.info['cmdline'] or [])}".lower()
                except Exception:
                    continue
                if any(m.lower() in hay for m in svc["process_match"] if m):
                    return "up", f"process {pr.info['name']} running"
            return "down", "no matching process"
        return "unknown", "no health URL or process pattern"

    # ---------- start rules ----------
    def can_start(self, p: dict | None = None) -> tuple[bool, str | None]:
        if p and p["kind"] == "service":
            return False, "deployed service — runs on its own"
        running = [x for x in self.jobs() if x["status"] in ACTIVE]
        if running:
            return False, f"'{running[0]['name']}' is running ({running[0]['owner']})"
        u = self.util()
        if u >= self.s.idle_threshold_percent:
            return False, f"GPU busy at {u:.0f}% (threshold {self.s.idle_threshold_percent:.0f}%){self.busiest()}"
        if p and u + p["est_gpu_percent"] > 100:
            return False, f"Not enough headroom: {u:.0f}% now + ~{p['est_gpu_percent']:.0f}% expected"
        if p and self.latest:
            g = self.latest[0]
            reserve = self.reserved_vram()
            free = g["memory_total_mb"] - g["memory_used_mb"] - reserve
            need = (p.get("est_vram_mb") or 0) * 1.05
            if need and free < need:
                why = f"Not enough VRAM: {max(free, 0) / 1024:.1f} GB free"
                why += f" (keeping {reserve / 1024:.0f} GB for services that are down)" if reserve else ""
                return False, why + f", needs ~{p['est_vram_mb'] / 1024:.1f} GB"
        return True, None

    def idle_for(self) -> float:
        return time.time() - self.idle_since if self.idle_since else 0.0

    # ---------- scheduler tick ----------
    def tick(self):
        with self.lock:
            now = datetime.now()
            projects = self.jobs()
            for p in projects:  # promote due scheduled jobs
                if p["status"] == "scheduled" and p["scheduled_at"] and p["scheduled_at"] <= now.timestamp():
                    self.db.update_project(p["id"], status="queued")
            projects = self.jobs()
            pending = [p for p in projects if p["status"] in scheduler.WAITING]
            running = [p for p in projects if p["status"] == "running"]
            self._check_stale(running)
            if running and pending:
                victim = running[0]
                short = scheduler.should_preempt(victim, pending, now, self.s)
                if short and not victim["stop_requested"]:
                    key = (victim["current_run_id"], short["id"])
                    seen = self.preempt_verdicts.get(key)
                    if seen and time.time() - seen[0] < self.s.preempt_recheck_seconds:
                        return
                    ok, source, why = decider.confirm_preempt(self.jev, victim, short, self.util(), now, self.s)
                    self.preempt_verdicts[key] = (time.time(), ok)
                    if ok:
                        self.last_decision = {"ts": time.time(), "project": f"pause {victim['name']} for {short['name']}",
                                              "source": source, "why": why, "tied": []}
                        self.event(f"Pausing '{victim['name']}' at next checkpoint so '{short['name']}' "
                                   f"(~{scheduler.remaining_minutes(short):.0f} min) can run; it resumes afterwards "
                                   f"[{source}: {why}]", project_id=victim["id"])
                        self.request_stop(victim["id"], requeue=True)
                    else:
                        self.event(f"Keeping '{victim['name']}' running; '{short['name']}' waits [{source}: {why}]",
                                   project_id=victim["id"])
                return
            if running or not pending:
                return
            ok, why = self.can_start()
            if not ok:
                return
            if self.idle_for() < self.s.idle_grace_seconds:
                return
            cands = scheduler.candidates_at(pending, now, now, self.s)
            if not cands:
                return
            chosen, source, reason = decider.pick_next(self.jev, cands, self.util(), now, self.s)
            ok, why = self.can_start(chosen)
            if not ok:
                return
            self.last_decision = {"ts": time.time(), "project": chosen["name"], "source": source, "why": reason,
                                  "tied": [t["name"] for t in cands[:6]]}
            self.event(f"Auto-start '{chosen['name']}' — GPU idle {self.idle_for():.0f}s at {self.util():.0f}% "
                       f"[{source}: {reason}]", project_id=chosen["id"])
            self._spawn(chosen, reason=f"auto ({source})")

    def _check_stale(self, running):
        for p in running:
            hb = p.get("last_heartbeat")
            if hb and time.time() - hb > self.s.heartbeat_timeout_seconds and "stale" not in (p["message"] or ""):
                self.db.update_project(p["id"], message=f"No heartbeat for {int(time.time() - hb)}s (stale?)")
                self.event(f"'{p['name']}' has not sent a heartbeat in {int(time.time() - hb)}s", "WARNING", p["id"])

    # ---------- actions ----------
    def start_now(self, pid: str, force=False) -> tuple[bool, str]:
        with self.lock:
            p = self.db.project(pid)
            if not p:
                return False, "unknown project"
            if p["status"] in ACTIVE:
                return False, "already running"
            if p["kind"] == "service":
                return False, "deployed services are not started by the coordinator"
            ok, why = self.can_start(p)
            if not ok and not force:
                return False, why
            self.event(f"Start Now '{p['name']}'" + (" (admin override)" if force and not ok else ""), project_id=pid)
            self._spawn(p, reason="manual" + (" override" if force and not ok else ""))
            return True, "started"

    def queue(self, pid: str, scheduled_at: float | None = None, priority=None, window=None):
        p = self.db.project(pid)
        if not p or p["status"] in ACTIVE or p["kind"] == "service":
            return False
        fields = {"status": "scheduled" if scheduled_at and scheduled_at > time.time() else "queued",
                  "scheduled_at": scheduled_at}
        if priority:
            fields["priority"] = priority
        if window:
            fields["preferred_window"] = window
        if p["status"] in ("completed", "failed", "cancelled"):
            fields.update(percent=0, step=0, active_seconds=0, resume_count=0, finished_at=None, message="")
        self.db.update_project(pid, **fields)
        when = datetime.fromtimestamp(scheduled_at).strftime("%a %d %b %H:%M") if scheduled_at else "next free slot"
        self.event(f"Queued '{p['name']}' for {when}", project_id=pid)
        return True

    def cancel(self, pid):
        p = self.db.project(pid)
        if p and p["status"] in scheduler.WAITING + ("paused",):
            self.db.update_project(pid, status="registered", scheduled_at=None)
            self.event(f"Removed '{p['name']}' from queue", project_id=pid)
            return True
        return False

    def request_stop(self, pid: str, requeue=False):
        p = self.db.project(pid)
        if not p or p["status"] not in ACTIVE:
            return False
        self.db.update_project(pid, stop_requested=2 if requeue else 1, status="stopping",
                               message="Checkpointing… (preempted)" if requeue else "Checkpointing… (pause requested)")
        try:
            stop_file = Path(p["path"]) / ".blackwell" / "STOP"
            stop_file.parent.mkdir(exist_ok=True)
            stop_file.write_text(str(time.time()))
        except OSError:
            pass
        proc = self.procs.get(pid)
        if proc and not p["supports_resume"]:
            _kill_tree(proc.pid)
        threading.Timer(self.s.stop_grace_seconds, self._force_kill, args=(pid, p["current_run_id"])).start()
        return True

    def _force_kill(self, pid, run_id):
        p = self.db.project(pid)
        proc = self.procs.get(pid)
        if p and proc and p["current_run_id"] == run_id and proc.poll() is None:
            self.event(f"'{p['name']}' did not checkpoint within {self.s.stop_grace_seconds}s — killing", "WARNING", pid)
            _kill_tree(proc.pid)

    # ---------- process management ----------
    def _spawn(self, p: dict, reason: str):
        resuming = (p["percent"] or 0) > 0 and p["supports_resume"]
        run_id = self.db.start_run(p["id"], reason, p["percent"] or 0)
        path = Path(p["path"])
        (path / ".blackwell" / "logs").mkdir(parents=True, exist_ok=True)
        (path / ".blackwell" / "STOP").unlink(missing_ok=True)
        env = {**os.environ, "BLACKWELL_PROJECT_ID": p["id"], "BLACKWELL_RUN_ID": run_id,
               "BLACKWELL_COORDINATOR_URL": f"http://127.0.0.1:{self.s.port}",
               "BLACKWELL_RESUME": "1" if resuming else "0", "PYTHONUNBUFFERED": "1",
               "PYTHONIOENCODING": "utf-8"}
        if self.s.api_token:
            env["BLACKWELL_API_TOKEN"] = self.s.api_token
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(p["entry_command"], shell=True, cwd=path, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=flags, start_new_session=sys.platform != "win32")
        self.procs[p["id"]] = proc
        self.run_gpu[p["id"]] = []
        base = self.latest[0]["memory_used_mb"] if self.latest else 0.0
        self.run_mem[p["id"]] = [base, base]
        self.idle_since = None
        self.db.update_project(p["id"], status="running", current_run_id=run_id, pid=proc.pid, stop_requested=0,
                               scheduled_at=None, last_heartbeat=time.time(), finished_at=None,
                               resume_count=(p["resume_count"] or 0) + (1 if resuming else 0),
                               message="Resuming from checkpoint" if resuming else "Starting")
        self.db.log(p["id"], f"Run {run_id} started ({reason}){' — resuming at %.1f%%' % p['percent'] if resuming else ''}",
                    run_id=run_id)
        threading.Thread(target=self._pump, args=(p["id"], run_id, proc, path), daemon=True).start()

    def _pump(self, pid, run_id, proc, path: Path):
        with open(path / ".blackwell" / "logs" / f"{run_id}.log", "a", encoding="utf-8") as fh:
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                fh.write(line + "\n")
                fh.flush()
                m = LEVEL_RE.search(line)
                lvl = {"WARN": "WARNING", "Traceback": "ERROR"}.get(m.group(1), m.group(1)) if m else "INFO"
                self.db.log(pid, line[:4000], lvl, source="stdout", run_id=run_id)

    def _reap(self):
        for pid, proc in list(self.procs.items()):
            code = proc.poll()
            if code is None:
                continue
            del self.procs[pid]
            p = self.db.project(pid)
            if not p:
                continue
            samples = self.run_gpu.pop(pid, [])
            mem = self.run_mem.pop(pid, None)
            avg = sum(samples) / len(samples) if samples else None
            stop = p["stop_requested"]
            if p["percent"] >= 99.5 or (code == 0 and not stop):
                status, msg = "completed", "Finished"
                self.db.update_project(pid, percent=100, finished_at=time.time())
            elif stop:
                status = "queued" if stop == 2 else "paused"
                msg = "Preempted at checkpoint — will resume automatically" if stop == 2 else "Paused at checkpoint"
                if not p["supports_resume"]:
                    msg += " (no resume support: restarts from 0)"
                    self.db.update_project(pid, percent=0, step=0, active_seconds=0)
            else:
                status, msg = "failed", f"Exited with code {code}"
            self.db.update_project(pid, status=status, message=msg, pid=None, current_run_id=None, stop_requested=0)
            if avg is not None and not self.mock and len(samples) > 10:
                learned = {"est_gpu_percent": round(0.5 * p["est_gpu_percent"] + 0.5 * avg, 1)}
                if mem and mem[1] - mem[0] > 256:
                    learned["est_vram_mb"] = round(max(mem[1] - mem[0], 0.7 * (p["est_vram_mb"] or 0)))
                self.db.update_project(pid, **learned)
            self.db.finish_run(p["current_run_id"], status, code, p["percent"], avg)
            self.event(f"'{p['name']}' → {status} ({msg}, exit {code})", "ERROR" if status == "failed" else "INFO", pid)

    # ---------- heartbeat from SDK ----------
    def heartbeat(self, pid: str, hb: dict) -> dict:
        p = self.db.project(pid)
        if not p:
            return {"action": "unknown_project"}
        fields = {"last_heartbeat": time.time()}
        for k in ("percent", "step", "total_steps"):
            if hb.get(k) is not None:
                fields[k] = float(hb[k])
        if hb.get("message"):
            fields["message"] = str(hb["message"])[:300]
        if hb.get("metrics"):
            fields["metrics"] = {**p["metrics"], **hb["metrics"]}
        if p["status"] == "stopping":
            fields.pop("message", None)
        self.db.update_project(pid, **fields)
        ev = hb.get("event")
        if ev in ("checkpoint", "milestone", "complete", "resume"):
            self.db.log(pid, f"[{ev}] {hb.get('message', '')}", source="sdk", run_id=hb.get("run_id"),
                        data={k: hb.get(k) for k in ("percent", "step", "metrics", "checkpoint")})
        return {"action": "stop" if p["stop_requested"] else "continue"}

    # ---------- views ----------
    def snapshot(self) -> dict:
        now = datetime.now()
        projects = self.jobs()
        timeline = scheduler.build_timeline(projects, now, self.s)
        tl = {e["project_id"]: e for e in timeline}
        running_any = any(p["status"] in ACTIVE for p in projects)
        out = []
        for p in projects:
            ok, why = self.can_start(p) if p["status"] not in ACTIVE else (False, "running")
            e = tl.get(p["id"])
            out.append({
                **{k: p[k] for k in ("id", "name", "path", "owner", "framework", "entry_command", "priority",
                                     "preferred_window", "supports_resume", "status", "scheduled_at", "percent",
                                     "step", "total_steps", "message", "metrics", "active_seconds", "resume_count",
                                     "est_gpu_percent", "est_vram_mb", "estimated_duration_min", "finished_at", "last_heartbeat")},
                "remaining_min": round(scheduler.remaining_minutes(p), 1),
                "projected_start": e["start"] if e else None,
                "projected_finish": e["end"] if e else (p["finished_at"] if p["status"] == "completed" else None),
                "plan_reason": e["reason"] if e else None,
                "is_long": scheduler.is_long(p, self.s),
                "can_start": ok, "blocked_reason": why,
            })
        g = self.latest[0] if self.latest else {}
        ok, why = self.can_start()
        pending = [p for p in projects if p["status"] in scheduler.WAITING]
        cands = scheduler.candidates_at(pending, now, now, self.s) if pending else []
        return {
            "ts": time.time(), "gpu": g, "gpus": self.latest, "mock_gpu": self.mock,
            "can_start_new_job": ok, "reason_if_blocked": why, "idle_for": round(self.idle_for()),
            "grace_seconds": self.s.idle_grace_seconds, "threshold": self.s.idle_threshold_percent,
            "night": {"active": scheduler.is_night(now, self.s), "start_hour": self.s.night_start_hour,
                      "end_hour": self.s.night_end_hour,
                      "next_start": scheduler.next_night_start(now, self.s).timestamp()},
            "running": running_any, "projects": out, "auto_candidate": cands[0]["name"] if cands else None, "timeline": timeline,
            "events": list(self.events)[:20], "last_decision": self.last_decision, "decider": self.s.decider,
            "jev": self.jev.status(),
            "util_raw": self.raw_util(), "util_effective": round(self.util(), 1), "service_util": round(self.service_util, 1),
            "exclude_service_load": self.s.exclude_service_load, "reserved_vram_mb": self.reserved_vram(),
            "gpu_procs": self.gpu_procs[:40], "services": [self._service_view(s) for s in self.services()],
        }

    def _service_view(self, svc: dict) -> dict:
        mine = [r for r in self.gpu_procs if r.get("project_id") == svc["id"]]
        return {**{k: svc[k] for k in ("id", "name", "path", "owner", "health_url", "process_match", "svc_state",
                                        "svc_since", "svc_checked", "svc_detail", "est_vram_mb", "framework")},
                "vram_mb": round(sum(r["mem_mb"] for r in mine)), "util": round(sum(r["util"] for r in mine), 1),
                "processes": [{"pid": r["pid"], "name": r["name"], "mem_mb": r["mem_mb"], "util": r["util"]} for r in mine]}


def _kill_tree(pid: int):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(pid, signal.SIGTERM)
    except Exception:
        pass
