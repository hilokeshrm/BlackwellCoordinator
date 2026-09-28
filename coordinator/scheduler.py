"""Deterministic scheduling heuristics. Pure functions over project dicts so they are easy to test."""
from datetime import datetime, timedelta

from .config import Settings

PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}
WAITING = ("queued", "scheduled")
SHORT_PREEMPT_MAX_MIN = 4 * 60


def remaining_minutes(p: dict) -> float:
    pct = float(p.get("percent") or 0)
    est = float(p.get("estimated_duration_min") or 60)
    active = float(p.get("active_seconds") or 0)
    if pct >= 100:
        return 0.0
    if pct >= 1 and active >= 30:
        observed = (active / 60) * (100 - pct) / pct
        # blend toward observation as progress grows
        w = min(1.0, pct / 10)
        return w * observed + (1 - w) * est * (1 - pct / 100)
    return est * (1 - pct / 100)


def is_night(dt: datetime, s: Settings) -> bool:
    h = dt.hour
    if s.night_start_hour > s.night_end_hour:
        return h >= s.night_start_hour or h < s.night_end_hour
    return s.night_start_hour <= h < s.night_end_hour


def next_night_start(dt: datetime, s: Settings) -> datetime:
    if is_night(dt, s):
        return dt
    start = dt.replace(hour=s.night_start_hour, minute=0, second=0, microsecond=0)
    return start if start > dt else start + timedelta(days=1)


def next_boundary(dt: datetime, s: Settings) -> datetime:
    """Next time the night/day state flips."""
    hour = s.night_end_hour if is_night(dt, s) else s.night_start_hour
    b = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    return b if b > dt else b + timedelta(days=1)


def is_long(p: dict, s: Settings) -> bool:
    return p.get("preferred_window") == "night_only" or remaining_minutes(p) > s.long_job_minutes


def window_ok(p: dict, dt: datetime, s: Settings) -> bool:
    w = p.get("preferred_window", "anytime")
    if w == "night_only":
        return is_night(dt, s)
    if w == "daytime_only":
        return not is_night(dt, s)
    return True


def _earliest(p: dict, now: datetime) -> datetime:
    sa = p.get("scheduled_at")
    if sa:
        t = datetime.fromtimestamp(sa)
        return t if t > now else now
    return now


def _sort_key(p, s):
    return (-PRIORITY_RANK.get(p.get("priority", "normal"), 1), remaining_minutes(p), p.get("created_at") or 0)


def candidates_at(pending: list[dict], t: datetime, now: datetime, s: Settings) -> list[dict]:
    """Jobs that the heuristic is willing to start at time t, best first."""
    ready = [p for p in pending if _earliest(p, now) <= t and window_ok(p, t, s)]
    if not ready:
        return []
    night = is_night(t, s)
    longs = [p for p in ready if is_long(p, s)]
    shorts = [p for p in ready if not is_long(p, s)]
    explicit = [p for p in ready if p.get("scheduled_at")]
    if explicit:  # a job explicitly scheduled for this slot wins
        return sorted(explicit, key=lambda p: _sort_key(p, s)) + [p for p in sorted(ready, key=lambda p: _sort_key(p, s)) if p not in explicit]
    if night:
        # night: long anchors first, they need the quiet hours
        return sorted(longs, key=lambda p: _sort_key(p, s)) + sorted(shorts, key=lambda p: _sort_key(p, s))
    # daytime: gap-fill with short jobs that finish before tonight's anchors
    anchors_waiting = any(is_long(p, s) for p in pending)
    if anchors_waiting and shorts:
        gap_end = next_night_start(t, s) - timedelta(minutes=s.gap_buffer_minutes)
        fitting = [p for p in shorts if t + timedelta(minutes=remaining_minutes(p)) <= gap_end]
        rest = [p for p in shorts if p not in fitting]
        return sorted(fitting, key=lambda p: _sort_key(p, s)) + sorted(rest, key=lambda p: _sort_key(p, s))
    if shorts:
        return sorted(shorts, key=lambda p: _sort_key(p, s))
    # only long "anytime" jobs left and GPU is free during the day: don't waste it
    return sorted([p for p in longs if p.get("preferred_window") != "night_only"], key=lambda p: _sort_key(p, s))


def build_timeline(projects: list[dict], now: datetime, s: Settings) -> list[dict]:
    """Projected execution plan: running job first, then waiting jobs placed greedily."""
    plan = []
    t = now
    for p in projects:
        if p["status"] in ("running", "stopping"):
            end = now + timedelta(minutes=remaining_minutes(p))
            plan.append(_entry(p, now, end, "running"))
            t = max(t, end)
    pending = [p for p in projects if p["status"] in WAITING]
    guard = 0
    while pending and guard < 500:
        guard += 1
        cands = candidates_at(pending, t, now, s)
        if cands:
            p = cands[0]
            end = t + timedelta(minutes=max(1.0, remaining_minutes(p)))
            reason = "night window" if is_night(t, s) else ("scheduled" if p.get("scheduled_at") else "gap-fill" if not is_long(p, s) else "idle GPU")
            plan.append(_entry(p, t, end, reason))
            pending.remove(p)
            t = end
            continue
        events = [next_boundary(t, s)] + [_earliest(p, now) for p in pending if _earliest(p, now) > t]
        t = min(events)
    return plan


def _entry(p, start, end, reason):
    return {
        "project_id": p["id"], "name": p["name"], "owner": p["owner"], "status": p["status"],
        "start": start.timestamp(), "end": end.timestamp(), "reason": reason,
        "duration_min": round((end - start).total_seconds() / 60, 1),
    }


def should_preempt(running: dict, pending: list[dict], now: datetime, s: Settings) -> dict | None:
    """Daytime: pause a resumable long job at a checkpoint so a short job can squeeze in, then resume."""
    if not s.preempt_for_short_jobs or not running.get("supports_resume"):
        return None
    if not is_long(running, s) or is_night(now, s):
        return None
    rank = PRIORITY_RANK.get(running.get("priority", "normal"), 1)
    for p in sorted(pending, key=lambda p: _sort_key(p, s)):
        if (not is_long(p, s) and remaining_minutes(p) <= SHORT_PREEMPT_MAX_MIN
                and PRIORITY_RANK.get(p.get("priority", "normal"), 1) >= rank
                and _earliest(p, now) <= now and window_ok(p, now, s)):
            return p
    return None
