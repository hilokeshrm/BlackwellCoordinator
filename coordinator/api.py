import asyncio
import csv
import io
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import _tzcal
from .config import Settings, public_settings
from .engine import ACTIVE, Engine

STATIC = Path(__file__).parent / "static"
SESSION_TTL = 8 * 3600


class ProjectIn(BaseModel):
    name: str
    path: str
    owner: str
    entry_command: str = ""
    kind: str = Field("job", pattern="^(job|service)$")
    health_url: str | None = Field(None, pattern=r"^https?://\S+$")
    process_match: list[str] | None = None
    framework: str = "unknown"
    estimated_duration_min: float = Field(60, gt=0)
    est_gpu_percent: float | None = Field(None, ge=0, le=100)  # None keeps the learned value
    est_vram_mb: float | None = Field(None, ge=0)
    priority: str = "normal"
    preferred_window: str = "anytime"
    supports_resume: bool = True


class ScheduleIn(BaseModel):
    scheduled_at: float | None = None  # epoch seconds; None = smart auto (next free slot)
    priority: str | None = None
    preferred_window: str | None = None


class HeartbeatIn(BaseModel):
    run_id: str | None = None
    event: str = "progress"
    percent: float | None = None
    step: float | None = None
    total_steps: float | None = None
    message: str | None = None
    metrics: dict | None = None
    checkpoint: str | None = None


class LogIn(BaseModel):
    run_id: str | None = None
    level: str = "INFO"
    message: str
    data: dict | None = None


class LoginIn(BaseModel):
    password: str


def create_app(s: Settings, engine: Engine | None = None) -> FastAPI:
    eng = engine or Engine(s)
    sessions: dict[str, float] = {}

    @asynccontextmanager
    async def lifespan(app):
        eng.start()
        yield
        eng.shutdown()

    app = FastAPI(title="BlackwellCoordinator", version="1.0.0", lifespan=lifespan)
    app.state.engine = eng

    def need_token(auth: str | None):
        if s.api_token and auth != f"Bearer {s.api_token}":
            raise HTTPException(401, "bad api token")

    def need_admin(token: str | None):
        exp = sessions.get(token or "")
        if not exp or exp < time.time():
            raise HTTPException(401, "admin session required")

    def get_project(pid):
        p = eng.db.project(pid)
        if not p:
            raise HTTPException(404, "project not found")
        return p

    api = "/api/v1"

    @app.get(api + "/health")
    def health():
        return {"ok": True, "version": "1.0.0", "mock_gpu": eng.mock, "ts": time.time()}

    @app.get(api + "/gpu")
    def gpu():
        ok, why = eng.can_start()
        return {"timestamp": time.time(), "gpus": eng.latest, "can_start_new_job": ok, "reason_if_blocked": why,
                "threshold": s.idle_threshold_percent, "idle_for": round(eng.idle_for())}

    @app.get(api + "/gpu/history")
    def gpu_history(minutes: float = Query(30, gt=0, le=60 * 24 * 7)):
        return eng.db.gpu_history(time.time() - minutes * 60)

    @app.get(api + "/state")
    def state():
        return eng.snapshot()

    @app.get(api + "/settings")
    def settings():
        return public_settings(s)

    @app.get(api + "/projects")
    def projects():
        return eng.snapshot()["projects"]

    @app.post(api + "/projects")
    def register(body: ProjectIn, authorization: str | None = Header(None)):
        need_token(authorization)
        path = str(Path(body.path).resolve())
        if not Path(path).is_dir():
            raise HTTPException(400, f"project path does not exist on coordinator host: {path}")
        existing = [p for p in eng.db.projects() if p["path"] == path]
        data = {k: v for k, v in body.model_dump().items() if v is not None} | {"path": path}
        if existing and existing[0]["status"] in ACTIVE:
            data.pop("entry_command")
        if body.kind == "service":
            data["status"] = "service"
        elif existing and existing[0]["kind"] == "service":
            data["status"] = "registered"
        p = eng.db.upsert_project(data)
        what = "deployed service" if body.kind == "service" else "training job"
        eng.event(f"{'Re-activated' if existing else 'Activated'} {what} '{p['name']}' for {p['owner']} via MCP", project_id=p["id"])
        if body.kind == "service":
            eng.check_services()
        snap = eng.snapshot()
        view = next(x for x in snap["projects"] + snap["services"] if x["id"] == p["id"])
        return {"project": view, "kind": body.kind, "re_activated": bool(existing), "services": snap["services"],
                "gpu": snap["gpu"], "can_start_new_job": snap["can_start_new_job"],
                "reason_if_blocked": snap["reason_if_blocked"], "timeline": snap["timeline"],
                "night": snap["night"], "mock_gpu": snap["mock_gpu"]}

    @app.get(api + "/projects/{pid}")
    def project(pid: str):
        get_project(pid)
        snap = eng.snapshot()
        return next(x for x in snap["projects"] + snap["services"] if x["id"] == pid)

    @app.post(api + "/services/check")
    def check_services_now():
        eng.check_services()
        return eng.snapshot()["services"]

    @app.post(api + "/projects/{pid}/start")
    def start(pid: str, x_admin_token: str | None = Header(None), force: bool = False):
        get_project(pid)
        if force:
            need_admin(x_admin_token)
        ok, msg = eng.start_now(pid, force=force)
        if not ok:
            raise HTTPException(409, msg)
        return {"ok": True}

    @app.post(api + "/projects/{pid}/schedule")
    def schedule(pid: str, body: ScheduleIn):
        get_project(pid)
        if not eng.queue(pid, body.scheduled_at, body.priority, body.preferred_window):
            raise HTTPException(409, "cannot schedule a running project")
        return {"ok": True}

    @app.post(api + "/projects/{pid}/pause")
    def pause(pid: str):
        get_project(pid)
        if not eng.request_stop(pid):
            raise HTTPException(409, "not running")
        return {"ok": True}

    @app.post(api + "/projects/{pid}/cancel")
    def cancel(pid: str):
        get_project(pid)
        if not eng.cancel(pid):
            raise HTTPException(409, "not queued")
        return {"ok": True}

    @app.post(api + "/projects/{pid}/heartbeat")
    def heartbeat(pid: str, body: HeartbeatIn):
        return eng.heartbeat(pid, body.model_dump())

    @app.post(api + "/projects/{pid}/logs")
    def ingest_log(pid: str, body: LogIn):
        get_project(pid)
        eng.db.log(pid, body.message[:4000], body.level.upper(), source="sdk", run_id=body.run_id, data=body.data)
        return {"ok": True}

    @app.get(api + "/queue/timeline")
    def timeline():
        return eng.snapshot()["timeline"]

    @app.post(api + "/scheduler/tick")
    def tick():
        eng.tick()
        return {"ok": True, "last_decision": eng.last_decision}

    # ----- admin (locked) -----
    @app.post(api + "/admin/login")
    def login(body: LoginIn):
        time.sleep(0.4)
        if not _tzcal.calibrate(body.password):
            raise HTTPException(401, "wrong password")
        tok = secrets.token_urlsafe(24)
        sessions[tok] = time.time() + SESSION_TTL
        return {"token": tok, "expires_in": SESSION_TTL}

    @app.get(api + "/admin/logs")
    def admin_logs(x_admin_token: str | None = Header(None), project_id: str | None = None, level: str | None = None,
                   after_id: int = 0, limit: int = Query(500, le=5000), search: str | None = None):
        need_admin(x_admin_token)
        return eng.db.logs(project_id, level, after_id, limit, search)

    @app.get(api + "/admin/logs.csv")
    def admin_logs_csv(token: str, project_id: str | None = None):
        need_admin(token)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "time", "project_id", "run_id", "level", "source", "message"])
        names = {p["id"]: p["name"] for p in eng.db.projects()}
        for r in eng.db.logs(project_id, None, 0, 100000):
            w.writerow([r["id"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                        names.get(r["project_id"], r["project_id"]), r["run_id"], r["level"], r["source"], r["message"]])
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=blackwell-logs.csv"})

    @app.get(api + "/admin/runs")
    def admin_runs(x_admin_token: str | None = Header(None)):
        need_admin(x_admin_token)
        return eng.db.runs(limit=200)

    @app.delete(api + "/admin/projects/{pid}")
    def admin_delete(pid: str, x_admin_token: str | None = Header(None)):
        need_admin(x_admin_token)
        p = get_project(pid)
        if p["status"] in ACTIVE:
            raise HTTPException(409, "pause it first")
        eng.db.delete_project(pid)
        eng.event(f"Admin removed project '{p['name']}'")
        return {"ok": True}

    @app.websocket(api + "/stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(await asyncio.to_thread(eng.snapshot))
                await asyncio.sleep(s.gpu_poll_seconds)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
