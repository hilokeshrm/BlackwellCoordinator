"""End-to-end: daemon (test GPU double) + MCP activate + SDK-instrumented sample projects as real subprocesses."""
import json
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from blackwell_mcp import server as mcp_server
from coordinator.api import create_app
from coordinator.config import Settings
from coordinator.engine import Engine


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def coord(tmp_path, monkeypatch):
    port = free_port()
    s = Settings(host="127.0.0.1", port=port, db_path=str(tmp_path / "c.db"), mock_gpu=True, idle_grace_seconds=2,
                 scheduler_tick_seconds=0.5, gpu_poll_seconds=0.5, decider="heuristic", stop_grace_seconds=30)
    monkeypatch.setenv("SAMPLE_EPOCH_SECONDS", "0.15")
    eng = Engine(s)
    srv = uvicorn.Server(uvicorn.Config(create_app(s, eng), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    monkeypatch.setattr(mcp_server, "COORD", f"http://127.0.0.1:{port}")
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=10)
    yield client, eng
    for p in eng.db.projects():
        if p["status"] in ("running", "stopping"):
            eng.request_stop(p["id"])
    srv.should_exit = True
    t.join(5)


def payload(report: str) -> dict:
    return json.loads(report.split("```json")[1].split("```")[0])


def wait_for(fn, timeout=30, every=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError("timed out")


def status(client, pid):
    return client.get(f"/projects/{pid}").json()


def test_health_and_gpu(coord):  # T1, T2
    client, _ = coord
    assert client.get("/health").json()["ok"]
    g1 = client.get("/gpu").json()
    time.sleep(1.1)
    assert client.get("/gpu").json()["gpus"][0]["ts"] > g1["gpus"][0]["ts"]
    assert len(client.get("/gpu/history?minutes=5").json()) >= 2


def test_activate_raw_project_returns_agent_tasks(coord, raw_finetune):  # T3
    client, _ = coord
    root = raw_finetune
    out = mcp_server.activate(str(root), owner="alice", estimated_duration_minutes=120)
    data = payload(out)
    assert data["status"] == "partial" and data["agent_tasks"]
    assert data["scan"]["entry_command"] == "python train.py --config config.json"
    assert data["scan"]["total_epochs"] == 20
    assert (root / "blackwell_sdk.py").exists()
    assert (root / ".cursor" / "rules" / "blackwell.mdc").exists()
    prof = json.loads((root / ".blackwell" / "profile.json").read_text())
    assert prof["project_id"] == data["project_id"] and prof["supports_resume"] is False
    assert ".blackwell/checkpoints/" in (root / ".gitignore").read_text()
    assert status(client, data["project_id"])["status"] == "registered"


def test_activate_is_idempotent_and_verifies(coord, finetune):
    client, _ = coord
    root = finetune
    a = payload(mcp_server.activate(str(root), owner="alice", estimated_duration_minutes=120))
    b = payload(mcp_server.activate(str(root), owner="alice"))
    assert a["project_id"] == b["project_id"]
    assert b["status"] == "complete" and b["supports_resume"]
    assert len(client.get("/projects").json()) == 1


def test_full_run_greyed_start_and_completion(coord, finetune, pretrain):  # T4, T7, T12
    client, eng = coord
    q = payload(mcp_server.activate(str(finetune), owner="alice", estimated_duration_minutes=120))["project_id"]
    l = payload(mcp_server.activate(str(pretrain), owner="bob", estimated_duration_minutes=1980))["project_id"]
    assert client.post(f"/projects/{q}/start").status_code == 200
    wait_for(lambda: status(client, q)["percent"] > 0)
    other = status(client, l)
    assert other["can_start"] is False and "running" in other["blocked_reason"]
    assert client.post(f"/projects/{l}/start").status_code == 409
    wait_for(lambda: status(client, q)["status"] == "completed", timeout=40)
    p = status(client, q)
    assert p["percent"] == 100 and p["active_seconds"] > 0
    wait_for(lambda: status(client, l)["can_start"], timeout=10)


def test_gpu_busy_blocks_start_and_autostart(coord, finetune, pretrain):  # T4
    client, eng = coord
    pid = payload(mcp_server.activate(str(finetune), owner="a", estimated_duration_minutes=60))["project_id"]
    eng.gpu.forced_util = 80
    time.sleep(1.2)
    r = client.post(f"/projects/{pid}/start")
    assert r.status_code == 409 and "busy" in r.json()["detail"]
    client.post(f"/projects/{pid}/schedule", json={})
    time.sleep(3)
    assert status(client, pid)["status"] == "queued"  # stays queued while someone else uses the GPU
    eng.gpu.forced_util = None
    wait_for(lambda: status(client, pid)["status"] in ("running", "completed"), timeout=15)  # auto-start after grace


def test_pause_checkpoint_and_resume(coord, finetune, pretrain):  # T8
    client, _ = coord
    root = pretrain
    pid = payload(mcp_server.activate(str(root), owner="bob", estimated_duration_minutes=1980))["project_id"]
    client.post(f"/projects/{pid}/start")
    wait_for(lambda: status(client, pid)["percent"] >= 5)
    assert client.post(f"/projects/{pid}/pause").status_code == 200
    wait_for(lambda: status(client, pid)["status"] == "paused", timeout=20)
    paused_at = status(client, pid)["percent"]
    ms = json.loads((root / ".blackwell" / "milestones.json").read_text())
    assert ms["can_resume"] and ms["step"] == pytest.approx(paused_at, abs=1)
    wait_for(lambda: status(client, pid)["can_start"], timeout=10)
    assert client.post(f"/projects/{pid}/start").status_code == 200
    wait_for(lambda: status(client, pid)["status"] == "completed", timeout=40)
    p = status(client, pid)
    assert p["resume_count"] == 1
    log = "".join(f.read_text() for f in (root / ".blackwell" / "logs").glob("*.log"))
    assert f"from epoch {int(paused_at)}" in log  # resumed, not restarted


def test_preemption_runs_short_job_then_resumes_long(coord, finetune, pretrain, monkeypatch):  # the 33h / 2h case
    client, eng = coord
    from coordinator import scheduler
    monkeypatch.setattr(scheduler, "is_night", lambda dt, s: False)
    l = payload(mcp_server.activate(str(pretrain), owner="bob", estimated_duration_minutes=1980))["project_id"]
    q = payload(mcp_server.activate(str(finetune), owner="alice", estimated_duration_minutes=120))["project_id"]
    client.post(f"/projects/{l}/start")
    wait_for(lambda: status(client, l)["percent"] >= 3)
    client.post(f"/projects/{q}/schedule", json={})
    wait_for(lambda: status(client, q)["status"] == "running", timeout=20)
    assert status(client, l)["status"] == "queued"  # preempted, auto-requeued
    wait_for(lambda: status(client, q)["status"] == "completed", timeout=40)
    wait_for(lambda: status(client, l)["status"] == "running", timeout=15)
    assert status(client, l)["resume_count"] == 1


def test_admin_logs_locked(coord, finetune, pretrain):  # T9
    client, _ = coord
    assert client.get("/admin/logs").status_code == 401
    assert client.post("/admin/login", json={"password": "nope"}).status_code == 401
    tok = client.post("/admin/login", json={"password": "atmiasri"}).json()["token"]
    rows = client.get("/admin/logs", headers={"X-Admin-Token": tok}).json()
    assert isinstance(rows, list)
    assert client.get(f"/admin/logs.csv?token={tok}").status_code == 200


def test_password_only_in_hidden_module():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    hits = [f.relative_to(root).as_posix() for f in root.rglob("*")
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(root).parts) and "tests" not in f.parts and f.suffix in (".py", ".js", ".html", ".css", ".json", ".md", ".ps1", ".env", ".yml")
            and "atmiasri" in f.read_text(encoding="utf-8", errors="ignore")]
    assert hits == ["coordinator/_tzcal.py"]
    assert "atmiasri" not in os.environ.values()


def test_timeline_endpoint(coord, finetune, pretrain):  # T11
    client, _ = coord
    a = payload(mcp_server.activate(str(pretrain), owner="bob", estimated_duration_minutes=1980,
                                    preferred_window="night_only"))["project_id"]
    client.post(f"/projects/{a}/schedule", json={})
    tl = client.get("/queue/timeline").json()
    assert tl[0]["project_id"] == a and tl[0]["reason"] == "night window"


def test_restart_recovery_requeues(tmp_path):
    s = Settings(db_path=str(tmp_path / "r.db"), mock_gpu=True)
    e1 = Engine(s)
    proj = tmp_path / "p"
    (proj / ".blackwell").mkdir(parents=True)
    p = e1.db.upsert_project({"name": "p", "path": str(proj), "owner": "x", "entry_command": "python x.py"})
    e1.db.update_project(p["id"], status="running", percent=40.0)
    e2 = Engine(s)
    assert e2.db.project(p["id"])["status"] == "queued"
    assert (proj / ".blackwell" / "STOP").exists()


def test_vram_blocks_start_and_reactivate_keeps_learned_values(coord, finetune):
    client, eng = coord
    pid = payload(mcp_server.activate(str(finetune), owner="a", estimated_duration_minutes=60, expected_vram_gb=200))["project_id"]
    p = status(client, pid)
    assert p["can_start"] is False and "VRAM" in p["blocked_reason"]
    assert client.post(f"/projects/{pid}/start").status_code == 409
    eng.db.update_project(pid, est_gpu_percent=42.0, est_vram_mb=2048)
    mcp_server.activate(str(finetune), owner="a")
    p = status(client, pid)
    assert p["est_gpu_percent"] == 42.0 and p["est_vram_mb"] == 2048 and p["can_start"]


def test_deployed_service_health_and_vram_reservation(coord, finetune, tmp_path):
    client, eng = coord
    base = str(client.base_url).rstrip("/")
    up_dir = tmp_path / "agent-platform"
    up_dir.mkdir()
    (up_dir / "compose.yml").write_text("services: {}\n")
    out = payload(mcp_server.activate(str(up_dir), owner="ops", health_url=f"{base}/health", expected_vram_gb=10))
    assert out["kind"] == "service" and out["state"] == "up"  # auto-detected from compose.yml
    assert not (up_dir / "blackwell_sdk.py").exists()
    down_dir = tmp_path / "inference-api"
    down_dir.mkdir()
    sid = payload(mcp_server.activate(str(down_dir), owner="ops", kind="service",
                                      health_url="http://127.0.0.1:9/health", expected_vram_gb=90))["project_id"]
    services = {s["id"]: s for s in client.get("/state").json()["services"]}
    assert services[sid]["svc_state"] == "down"
    assert client.post(f"/projects/{sid}/start").status_code == 409
    assert client.post(f"/projects/{sid}/schedule", json={}).status_code == 409
    jid = payload(mcp_server.activate(str(finetune), owner="a", estimated_duration_minutes=60, expected_vram_gb=20))["project_id"]
    p = status(client, jid)
    assert not p["can_start"] and "services that are down" in p["blocked_reason"]
    state = client.get("/state").json()
    assert all(x["id"] != sid for x in state["projects"]) and state["reserved_vram_mb"] == 90 * 1024
