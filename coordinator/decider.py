"""Scheduling decisions. Hard rules and heuristics filter what is allowed; Open-Jev picks among the allowed
options (calibrated probabilities, cannot answer outside the offered options). Ollama, then the heuristic
order, are fallbacks when Jev is down or unsure."""
import json
import re
import threading
import time
from datetime import datetime

import httpx

from .config import Settings
from .scheduler import PRIORITY_RANK, is_long, is_night, next_night_start, remaining_minutes

WARMUP = {"state": "GPU idle.", "questions": {"q": {"type": "choice", "instructions": "Pick one.",
                                                    "criteria": {"a": "start job a", "b": "start job b"}}}}


class JevClient:
    """Open-Jev decision server (same API the m3 jev-router serves): POST /v1/systemone."""

    def __init__(self, s: Settings):
        self.s = s
        self.online: bool | None = None
        self.last_error = ""
        self.last_ok = 0.0
        self._lock = threading.Lock()

    @property
    def url(self):
        return self.s.jev_url.rstrip("/") + "/v1/systemone"

    def warm(self):
        """First question compiles kernels (~25 s), so do it off the scheduling path."""
        try:
            httpx.post(self.url, json=WARMUP, timeout=120).raise_for_status()
            self._mark(True)
        except Exception as e:
            self._mark(False, e)

    def choice(self, state: str, instructions: str, criteria: dict[str, str]) -> tuple[str, float] | None:
        body = {"state": state, "questions": {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}}}
        try:
            r = httpx.post(self.url, json=body, timeout=self.s.jev_timeout_seconds)
            r.raise_for_status()
            ans = r.json()["answers"]["q"]
            pick = str(ans["choice"])
            prob = float(ans["probabilities"][pick])
        except Exception as e:
            self._mark(False, e)
            return None
        self._mark(True)
        return (pick, prob) if pick in criteria else None

    def _mark(self, ok: bool, err=None):
        with self._lock:
            self.online = ok
            if ok:
                self.last_ok = time.time()
                self.last_error = ""
            else:
                self.last_error = f"{type(err).__name__}: {err}"[:200] if err else "unavailable"

    def status(self) -> dict:
        return {"url": self.s.jev_url, "online": self.online, "last_error": self.last_error, "last_ok": self.last_ok}


def _fmt_min(m: float) -> str:
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"


def _facts(p: dict, now: datetime, s: Settings, rank: int) -> str:
    rem = remaining_minutes(p)
    fits = now.timestamp() + rem * 60 <= next_night_start(now, s).timestamp()
    waited = max(0, (now.timestamp() - (p.get("updated_at") or now.timestamp())) / 60)
    return (f"{p['name']} owned by {p['owner']}: {p.get('priority', 'normal')} priority, about {_fmt_min(rem)} left, "
            f"{p.get('percent') or 0:.0f}% done, interrupted and resumed {p.get('resume_count') or 0} times, "
            f"waiting {_fmt_min(waited)}, {'long job' if is_long(p, s) else 'short job'}, "
            f"{'finishes before tonight' if fits else 'runs past tonight'}, heuristic rank {rank}.")


def _state(now: datetime, gpu_util: float, s: Settings) -> str:
    night = is_night(now, s)
    return (f"Shared single-GPU workstation for an ML team. Local time {now:%a %H:%M}. GPU utilisation {gpu_util:.0f}%, "
            f"no job running. Night window {s.night_start_hour:02d}:00-{s.night_end_hour:02d}:00 is "
            f"{'active now' if night else 'later today'}; long jobs belong in the night window, short jobs fill the "
            f"daytime gaps before it.")


PICK_INSTRUCTIONS = (
    "Which queued job should start on the GPU right now? Respect priority first. By day, prefer a short job that "
    "finishes before tonight so long jobs get the night; at night prefer long jobs. Prefer finishing jobs that were "
    "interrupted, then the job waiting longest, and keep it fair between owners.")


def _ollama_pick(state: str, criteria: dict[str, str], s: Settings) -> str | None:
    prompt = (state + "\n\n" + PICK_INSTRUCTIONS + "\nOptions:\n" + "\n".join(f"- {k}: {v}" for k, v in criteria.items())
              + '\nReply JSON only: {"choice": "<option key>"}')
    try:
        r = httpx.post(f"{s.ollama_url}/api/generate", timeout=20, json={
            "model": s.ollama_model, "prompt": prompt, "stream": False, "format": "json", "options": {"temperature": 0}})
        m = re.search(r"\{.*\}", r.json()["response"], re.S)
        pick = json.loads(m.group(0)).get("choice") if m else None
        return pick if pick in criteria else None
    except Exception:
        return None


def pick_next(jev: JevClient, cands: list[dict], gpu_util: float, now: datetime, s: Settings) -> tuple[dict, str, str]:
    """cands: jobs the hard rules allow right now, best heuristic first. Returns (job, source, why)."""
    if len(cands) == 1 or s.decider == "heuristic":
        return cands[0], "heuristic", "only allowed job" if len(cands) == 1 else "heuristic order"
    top = cands[:6]
    criteria, by_key = {}, {}
    for i, p in enumerate(top, 1):
        key = f"{p['name']} ({p['owner']})"
        while key in criteria:
            key += "'"
        criteria[key] = _facts(p, now, s, i)
        by_key[key] = p
    state = _state(now, gpu_util, s)
    if s.decider in ("auto", "jev"):
        ans = jev.choice(state, PICK_INSTRUCTIONS, criteria)
        if ans:
            key, prob = ans
            chosen = by_key[key]
            # never let Jev jump a strictly higher priority job
            best_pri = max(PRIORITY_RANK.get(p.get("priority"), 1) for p in top)
            if prob >= s.jev_min_probability and PRIORITY_RANK.get(chosen.get("priority"), 1) == best_pri:
                return chosen, f"jev p={prob:.2f}", "Jev pick" + ("" if chosen is top[0] else " (overrode heuristic order)")
            return top[0], "heuristic", f"Jev unsure (p={prob:.2f} for {chosen['name']}) — heuristic order"
    if s.decider in ("auto", "ollama"):
        key = _ollama_pick(state, criteria, s)
        if key:
            chosen = by_key[key]
            best_pri = max(PRIORITY_RANK.get(p.get("priority"), 1) for p in top)
            if PRIORITY_RANK.get(chosen.get("priority"), 1) == best_pri:
                return chosen, f"ollama:{s.ollama_model}", "local LLM pick (Jev offline)"
    return top[0], "heuristic", "heuristic order (Jev offline)"


def confirm_preempt(jev: JevClient, running: dict, short: dict, gpu_util: float, now: datetime, s: Settings) -> tuple[bool, str, str]:
    """Heuristics found a short job that could run if the long job pauses at a checkpoint. Jev confirms."""
    if s.decider not in ("auto", "jev"):
        return True, "heuristic", "short job fits, long job is resumable"
    state = (_state(now, gpu_util, s).replace("no job running", f"'{running['name']}' is running") +
             f"\nRunning: {_facts(running, now, s, 1)}\nWaiting: {_facts(short, now, s, 1)}\n"
             "Pausing saves a checkpoint and loses at most a few minutes; the paused job resumes automatically "
             "after the waiting job finishes.")
    ans = jev.choice(state, "Should the running job pause now so the waiting job can run first?", {
        "pause": f"Pause {running['name']} at its next checkpoint, run {short['name']}, then resume {running['name']}.",
        "keep": f"Keep {running['name']} running; {short['name']} waits until it finishes.",
    })
    if not ans:
        return True, "heuristic", "Jev offline — short job fits, long job is resumable"
    pick, prob = ans
    if pick == "pause":
        return True, f"jev p={prob:.2f}", "Jev agreed to pause"
    if prob >= s.jev_min_probability:
        return False, f"jev p={prob:.2f}", "Jev chose to keep the running job"
    return True, "heuristic", f"Jev unsure (p={prob:.2f}) — heuristic pauses"
