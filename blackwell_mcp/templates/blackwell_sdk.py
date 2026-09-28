"""BlackwellCoordinator project SDK — checkpoint/resume, milestones, heartbeats, graceful stop.

Added by the Blackwell MCP `activate` tool. Zero dependencies (stdlib only). Typical use:

    from blackwell_sdk import Blackwell
    bw = Blackwell(total_steps=num_epochs)
    ckpt = bw.latest_checkpoint()                  # None on a fresh run
    start = 0
    if ckpt:
        model.load_state_dict(torch.load(ckpt.path)); start = ckpt.step
    for epoch in range(start, num_epochs):
        loss = train_one_epoch()
        bw.progress(epoch + 1, metrics={"loss": loss})
        if bw.checkpoint_due(epoch + 1, every=5) or bw.should_stop:
            bw.checkpoint(epoch + 1, lambda path: torch.save(model.state_dict(), path))
        if bw.should_stop:
            bw.exit_paused()
    bw.complete()
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SDK_VERSION = "1.0.0"


@dataclass
class Checkpoint:
    path: str
    step: float
    percent: float
    meta: dict


class Blackwell:
    def __init__(self, total_steps: float, project_dir: str | os.PathLike | None = None,
                 heartbeat_every: float = 15.0, keep_last: int = 3):
        self.root = Path(project_dir or os.getcwd()).resolve()
        self.dir = self.root / ".blackwell"
        self.ckpt_dir = self.dir / "checkpoints"
        self.ckpt_dir.mkdir(parents=True, exist_ok=True)
        self.milestones_file = self.dir / "milestones.json"
        self.total = float(total_steps)
        self.keep_last = keep_last
        self.step = 0.0
        self.metrics: dict = {}
        self._stop = threading.Event()
        self._last_hb = 0.0
        self._msg = ""

        self.project_id = os.environ.get("BLACKWELL_PROJECT_ID") or self._profile().get("project_id")
        self.run_id = os.environ.get("BLACKWELL_RUN_ID")
        self.url = os.environ.get("BLACKWELL_COORDINATOR_URL") or self._profile().get("coordinator_url")
        self.token = os.environ.get("BLACKWELL_API_TOKEN", "")
        self.managed = bool(self.run_id)  # launched by the coordinator
        self.fresh = self.managed and os.environ.get("BLACKWELL_RESUME") == "0"
        if self.fresh:
            self._reset_milestones()
        if not self.managed:
            self.log("Not launched by BlackwellCoordinator — checkpoints work, heartbeats disabled. "
                     "Use the dashboard (Start Now / Schedule) to run on the shared GPU.", "WARNING")
        self._install_signals()
        if self.managed:
            threading.Thread(target=self._hb_loop, args=(heartbeat_every,), daemon=True).start()
        ck = self.latest_checkpoint()
        if ck:
            self.step = ck.step
            self.log(f"Resumable checkpoint found at step {ck.step:g}/{self.total:g} ({ck.percent:.1f}%)")
            self._send("resume", f"resuming from step {ck.step:g}")

    # ------------------------------------------------------------------ progress
    @property
    def percent(self) -> float:
        return round(min(100.0, 100.0 * self.step / self.total), 2) if self.total else 0.0

    @property
    def should_stop(self) -> bool:
        if not self._stop.is_set() and (self.dir / "STOP").exists():
            self._stop.set()
        return self._stop.is_set()

    def progress(self, step: float, metrics: dict | None = None, message: str | None = None):
        self.step = float(step)
        if metrics:
            self.metrics.update({k: _num(v) for k, v in metrics.items()})
        self._msg = message or f"step {step:g}/{self.total:g}" + "".join(
            f" {k}={v:.4g}" if isinstance(v, float) else f" {k}={v}" for k, v in (metrics or {}).items())
        if time.time() - self._last_hb >= 2:
            self._send("progress", self._msg)

    def milestone(self, name: str, **data):
        self.log(f"Milestone: {name}")
        m = self._read_milestones()
        m.setdefault("milestones", []).append({"name": name, "step": self.step, "ts": _now(), **data})
        self._write_milestones(m)
        self._send("milestone", name)

    def log(self, message: str, level: str = "INFO", **metrics):
        extra = " ".join(f"{k}={v}" for k, v in metrics.items())
        print(f"[blackwell] {level} {message}{(' ' + extra) if extra else ''}", flush=True)

    # ------------------------------------------------------------------ checkpoints
    def checkpoint_due(self, step: float, every: float = 1) -> bool:
        return every > 0 and step % every == 0

    def checkpoint(self, step: float, save_fn, name: str | None = None, **meta) -> Checkpoint:
        """save_fn(path) must write the checkpoint to `path`. Written atomically via a temp file."""
        self.step = float(step)
        fname = name or f"step_{int(step):08d}.ckpt"
        final = self.ckpt_dir / fname
        tmp = self.ckpt_dir / (fname + ".tmp")
        save_fn(str(tmp))
        os.replace(tmp, final)
        m = self._read_milestones()
        m.update(last_checkpoint=_now(), checkpoint_path=str(final.relative_to(self.root)), step=self.step,
                 total_steps=self.total, percent_complete=self.percent, can_resume=True, meta=meta)
        hist = m.setdefault("history", [])
        hist.append({"path": m["checkpoint_path"], "step": self.step, "ts": m["last_checkpoint"]})
        for old in hist[:-self.keep_last]:
            (self.root / old["path"]).unlink(missing_ok=True)
        m["history"] = hist[-self.keep_last:]
        self._write_milestones(m)
        self.log(f"Checkpoint saved: {final.name} ({self.percent:.1f}%)")
        self._send("checkpoint", f"checkpoint {final.name}", checkpoint=str(final))
        return Checkpoint(str(final), self.step, self.percent, meta)

    def checkpoint_json(self, step: float, state: dict, **meta) -> Checkpoint:
        return self.checkpoint(step, lambda p: Path(p).write_text(json.dumps(state)), **meta)

    def latest_checkpoint(self) -> Checkpoint | None:
        m = self._read_milestones()
        if not m.get("can_resume") or not m.get("checkpoint_path"):
            return None
        p = self.root / m["checkpoint_path"]
        if not p.exists():
            return None
        return Checkpoint(str(p), float(m.get("step", 0)), float(m.get("percent_complete", 0)), m.get("meta", {}))

    def load_json(self) -> dict | None:
        ck = self.latest_checkpoint()
        return json.loads(Path(ck.path).read_text()) if ck else None

    # ------------------------------------------------------------------ exits
    def complete(self, message: str = "training complete"):
        self.step = self.total
        m = self._read_milestones()
        m.update(completed_at=_now(), percent_complete=100.0, can_resume=False)
        self._write_milestones(m)
        self.log(message)
        self._send("complete", message)

    def exit_paused(self, code: int = 0):
        self.log(f"Stop requested — exiting cleanly at {self.percent:.1f}%; the coordinator will resume later")
        self._send("progress", "paused at checkpoint")
        sys.exit(code)

    # ------------------------------------------------------------------ internals
    def _install_signals(self):
        def handler(signum, _frame):
            self.log(f"Received signal {signum}; will checkpoint and stop at the next safe point", "WARNING")
            self._stop.set()
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass

    def _hb_loop(self, every):
        while True:
            time.sleep(every)
            self._send("progress", self._msg or "alive")

    def _send(self, event: str, message: str, **extra):
        if not (self.managed and self.url and self.project_id):
            return
        self._last_hb = time.time()
        body = {"run_id": self.run_id, "event": event, "percent": self.percent, "step": self.step,
                "total_steps": self.total, "message": message, "metrics": self.metrics, **extra}
        req = urllib.request.Request(f"{self.url}/api/v1/projects/{self.project_id}/heartbeat",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                if json.loads(r.read() or b"{}").get("action") == "stop":
                    self._stop.set()
        except Exception:
            pass  # coordinator unreachable: keep training

    def _profile(self) -> dict:
        f = Path(os.getcwd()) / ".blackwell" / "profile.json"
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return {}

    def _read_milestones(self) -> dict:
        try:
            return json.loads(self.milestones_file.read_text())
        except (OSError, ValueError):
            return {}

    def _write_milestones(self, m: dict):
        tmp = self.milestones_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=2))
        os.replace(tmp, self.milestones_file)

    def _reset_milestones(self):
        m = self._read_milestones()
        for h in m.get("history", []):
            (self.root / h["path"]).unlink(missing_ok=True)
        self._write_milestones({"reset_at": _now()})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v
