from datetime import datetime

from coordinator import decider
from coordinator.config import Settings
from coordinator.scheduler import (build_timeline, candidates_at, is_night, remaining_minutes, should_preempt)

S = Settings()
MON_2PM = datetime(2026, 9, 28, 14, 0)
MON_11PM = datetime(2026, 9, 28, 23, 0)


def job(id, minutes, status="queued", priority="normal", window="anytime", **kw):
    return {"id": id, "name": id, "owner": "t", "status": status, "estimated_duration_min": minutes,
            "percent": 0, "active_seconds": 0, "priority": priority, "preferred_window": window,
            "supports_resume": True, "scheduled_at": None, "created_at": 0, "updated_at": 0, "resume_count": 0, **kw}


def test_night_window():
    assert is_night(MON_11PM, S)
    assert is_night(datetime(2026, 9, 29, 6, 59), S)
    assert not is_night(MON_2PM, S)


def test_gap_fill_short_before_long_night_job():  # PRD Appendix A, scenario 1
    q = [job("train-33h", 33 * 60, window="night_only"), job("finetune-2h", 120, priority="high")]
    plan = build_timeline(q, MON_2PM, S)
    assert [p["project_id"] for p in plan] == ["finetune-2h", "train-33h"]
    assert datetime.fromtimestamp(plan[0]["start"]) == MON_2PM
    assert datetime.fromtimestamp(plan[1]["start"]).hour == 22
    assert plan[0]["reason"] == "gap-fill" and plan[1]["reason"] == "night window"


def test_long_anytime_job_waits_while_short_jobs_exist():
    q = [job("long", 33 * 60), job("short", 120)]
    assert candidates_at(q, MON_2PM, MON_2PM, S)[0]["id"] == "short"


def test_long_anytime_job_uses_idle_day_gpu_when_alone():
    assert candidates_at([job("long", 33 * 60)], MON_2PM, MON_2PM, S)[0]["id"] == "long"


def test_night_only_never_starts_in_daytime():
    assert candidates_at([job("n", 60, window="night_only")], MON_2PM, MON_2PM, S) == []


def test_at_night_long_anchor_goes_first():
    q = [job("short", 60), job("long", 20 * 60)]
    assert candidates_at(q, MON_11PM, MON_11PM, S)[0]["id"] == "long"


def test_priority_beats_size():
    q = [job("small", 30), job("urgent", 180, priority="critical")]
    assert candidates_at(q, MON_2PM, MON_2PM, S)[0]["id"] == "urgent"


def test_scheduled_time_respected():
    later = datetime(2026, 9, 28, 17, 0).timestamp()
    q = [job("a", 60, status="scheduled", scheduled_at=later)]
    assert candidates_at(q, MON_2PM, MON_2PM, S) == []
    plan = build_timeline(q, MON_2PM, S)
    assert datetime.fromtimestamp(plan[0]["start"]).hour == 17


def test_running_job_pushes_queue():
    q = [job("run", 120, status="running", percent=50.0), job("next", 60)]
    plan = build_timeline(q, MON_2PM, S)
    assert plan[0]["project_id"] == "run"
    assert datetime.fromtimestamp(plan[1]["start"]) == datetime(2026, 9, 28, 15, 0)


def test_remaining_uses_observed_rate():
    p = job("x", 600, percent=50.0, active_seconds=3600)  # observed: 60 min per 50%
    assert abs(remaining_minutes(p) - 60) < 1


def test_preempt_long_for_short_in_daytime_only():
    running = job("long", 33 * 60, status="running", percent=10.0)
    short = job("short", 120)
    assert should_preempt(running, [short], MON_2PM, S)["id"] == "short"
    assert should_preempt(running, [short], MON_11PM, S) is None
    assert should_preempt({**running, "supports_resume": False}, [short], MON_2PM, S) is None
    assert should_preempt(running, [job("low", 60, priority="low")], MON_2PM, S) is None


class FakeJev:
    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def choice(self, state, instructions, criteria):
        self.calls.append(criteria)
        if self.answer is None:
            return None
        pick, prob = self.answer
        key = next(k for k in criteria if k.startswith(pick))
        return key, prob


def test_jev_picks_among_allowed_jobs():
    s = Settings(decider="jev")
    cands = [job("a", 60), job("b", 62, resume_count=1)]
    chosen, src, _ = decider.pick_next(FakeJev(("b", 0.91)), cands, 5, MON_2PM, s)
    assert chosen["id"] == "b" and src.startswith("jev")


def test_jev_low_confidence_falls_back_to_heuristic():
    s = Settings(decider="jev")
    chosen, src, why = decider.pick_next(FakeJev(("b", 0.30)), [job("a", 60), job("b", 60)], 5, MON_2PM, s)
    assert chosen["id"] == "a" and src == "heuristic" and "unsure" in why


def test_jev_cannot_skip_higher_priority():
    s = Settings(decider="jev")
    cands = [job("urgent", 60, priority="high"), job("b", 60)]
    chosen, _, _ = decider.pick_next(FakeJev(("b", 0.99)), cands, 5, MON_2PM, s)
    assert chosen["id"] == "urgent"


def test_single_candidate_skips_jev():
    fake = FakeJev(("a", 0.9))
    chosen, src, _ = decider.pick_next(fake, [job("a", 60)], 5, MON_2PM, Settings())
    assert chosen["id"] == "a" and src == "heuristic" and not fake.calls


def test_jev_offline_falls_back():
    s = Settings(decider="jev")
    chosen, src, _ = decider.pick_next(FakeJev(None), [job("a", 60), job("b", 60)], 5, MON_2PM, s)
    assert chosen["id"] == "a" and src == "heuristic"


def test_preempt_confirmation():
    s = Settings(decider="jev")
    run, short = job("long", 33 * 60, status="running", percent=10.0), job("short", 120)
    assert decider.confirm_preempt(FakeJev(("pause", 0.8)), run, short, 90, MON_2PM, s)[0] is True
    assert decider.confirm_preempt(FakeJev(("keep", 0.9)), run, short, 90, MON_2PM, s)[0] is False
    assert decider.confirm_preempt(FakeJev(("keep", 0.4)), run, short, 90, MON_2PM, s)[0] is True
    assert decider.confirm_preempt(FakeJev(None), run, short, 90, MON_2PM, s)[0] is True


def test_jev_client_speaks_open_jev_api():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.update(path=self.path, body=body)
            keys = list(body["questions"]["q"]["criteria"])
            out = {"answers": {"q": {"choice": keys[1], "probabilities": {keys[0]: 0.1, keys[1]: 0.9}}}}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = decider.JevClient(Settings(jev_url=f"http://127.0.0.1:{srv.server_port}"))
        assert client.choice("state", "pick", {"x": "first", "y": "second"}) == ("y", 0.9)
        assert seen["path"] == "/v1/systemone" and seen["body"]["questions"]["q"]["type"] == "choice"
        assert client.status()["online"] is True
    finally:
        srv.shutdown()
    dead = decider.JevClient(Settings(jev_url="http://127.0.0.1:9", jev_timeout_seconds=0.5))
    assert dead.choice("s", "i", {"a": "a", "b": "b"}) is None and dead.status()["online"] is False
