import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    framework TEXT DEFAULT 'unknown',
    entry_command TEXT NOT NULL,
    estimated_duration_min REAL DEFAULT 60,
    est_gpu_percent REAL DEFAULT 90,
    est_vram_mb REAL DEFAULT 0,
    priority TEXT DEFAULT 'normal',
    preferred_window TEXT DEFAULT 'anytime',
    supports_resume INTEGER DEFAULT 1,
    status TEXT DEFAULT 'registered',
    scheduled_at REAL,
    percent REAL DEFAULT 0,
    step REAL DEFAULT 0,
    total_steps REAL DEFAULT 0,
    message TEXT DEFAULT '',
    metrics TEXT DEFAULT '{}',
    active_seconds REAL DEFAULT 0,
    current_run_id TEXT,
    last_heartbeat REAL,
    stop_requested INTEGER DEFAULT 0,
    resume_count INTEGER DEFAULT 0,
    pid INTEGER,
    finished_at REAL,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT,
    reason TEXT,
    started_at REAL,
    finished_at REAL,
    exit_code INTEGER,
    start_percent REAL DEFAULT 0,
    end_percent REAL,
    avg_gpu_percent REAL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    run_id TEXT,
    ts REAL,
    level TEXT,
    source TEXT,
    message TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS logs_proj ON logs(project_id, id);
CREATE TABLE IF NOT EXISTS gpu_samples (ts REAL, util REAL, mem_used_mb REAL, mem_total_mb REAL);
CREATE INDEX IF NOT EXISTS gpu_ts ON gpu_samples(ts);
"""

MIGRATIONS = [
    ("est_vram_mb", "REAL DEFAULT 0"),
    ("kind", "TEXT DEFAULT 'job'"),  # job (queued, run by the coordinator) | service (deployed, always on)
    ("health_url", "TEXT"),
    ("process_match", "TEXT DEFAULT '[]'"),
    ("svc_state", "TEXT DEFAULT 'unknown'"),
    ("svc_since", "REAL"),
    ("svc_checked", "REAL"),
    ("svc_detail", "TEXT DEFAULT ''"),
]

PROJECT_FIELDS = {
    "name", "path", "owner", "framework", "entry_command", "estimated_duration_min", "est_gpu_percent", "est_vram_mb",
    "priority", "preferred_window", "supports_resume", "status", "scheduled_at", "percent", "step",
    "total_steps", "message", "metrics", "active_seconds", "current_run_id", "last_heartbeat",
    "stop_requested", "resume_count", "finished_at", "pid", "kind", "health_url", "process_match", "svc_state",
    "svc_since", "svc_checked", "svc_detail",
}


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(projects)")}
            for col, decl in MIGRATIONS:
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {decl}")

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    # projects
    def upsert_project(self, data: dict) -> dict:
        now = time.time()
        existing = self.q("SELECT * FROM projects WHERE path=?", (data["path"],))
        fields = {k: v for k, v in data.items() if k in PROJECT_FIELDS}
        if "process_match" in fields and not isinstance(fields["process_match"], str):
            fields["process_match"] = json.dumps(fields["process_match"])
        if existing:
            pid = existing[0]["id"]
            self.update_project(pid, **fields)
        else:
            pid = data.get("id") or uuid.uuid4().hex[:12]
            fields.update(created_at=now, updated_at=now)
            cols = ["id", *fields]
            self.x(f"INSERT INTO projects ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   [pid, *fields.values()])
        return self.project(pid)

    def update_project(self, project_id: str, /, **fields):
        fields = {k: v for k, v in fields.items() if k in PROJECT_FIELDS}
        for k in ("metrics", "process_match"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE projects SET {sets} WHERE id=?", [*fields.values(), project_id])

    def project(self, pid: str) -> dict | None:
        r = self.q("SELECT * FROM projects WHERE id=?", (pid,))
        return _decode(r[0]) if r else None

    def projects(self) -> list[dict]:
        return [_decode(r) for r in self.q("SELECT * FROM projects ORDER BY created_at")]

    def delete_project(self, pid: str):
        self.x("DELETE FROM projects WHERE id=?", (pid,))

    # runs
    def start_run(self, pid: str, reason: str, start_percent: float) -> str:
        rid = uuid.uuid4().hex[:12]
        self.x("INSERT INTO runs (id, project_id, status, reason, started_at, start_percent) VALUES (?,?,?,?,?,?)",
               (rid, pid, "running", reason, time.time(), start_percent))
        return rid

    def finish_run(self, rid: str, status: str, exit_code, end_percent, avg_gpu):
        self.x("UPDATE runs SET status=?, finished_at=?, exit_code=?, end_percent=?, avg_gpu_percent=? WHERE id=?",
               (status, time.time(), exit_code, end_percent, avg_gpu, rid))

    def runs(self, pid: str | None = None, limit: int = 50):
        if pid:
            return self.q("SELECT * FROM runs WHERE project_id=? ORDER BY started_at DESC LIMIT ?", (pid, limit))
        return self.q("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))

    # logs
    def log(self, project_id, message, level="INFO", source="daemon", run_id=None, data=None):
        self.x("INSERT INTO logs (project_id, run_id, ts, level, source, message, data) VALUES (?,?,?,?,?,?,?)",
               (project_id, run_id, time.time(), level, source, message, json.dumps(data) if data else None))

    def logs(self, project_id=None, level=None, after_id=0, limit=500, search=None):
        sql, args = "SELECT * FROM logs WHERE id>?", [after_id]
        if project_id:
            sql += " AND project_id=?"
            args.append(project_id)
        if level:
            sql += " AND level=?"
            args.append(level)
        if search:
            sql += " AND message LIKE ?"
            args.append(f"%{search}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return list(reversed(self.q(sql, args)))

    # gpu
    def add_gpu_sample(self, ts, util, used, total):
        self.x("INSERT INTO gpu_samples VALUES (?,?,?,?)", (ts, util, used, total))

    def gpu_history(self, since: float, max_points: int = 600):
        rows = self.q("SELECT ts, util, mem_used_mb, mem_total_mb FROM gpu_samples WHERE ts>=? ORDER BY ts", (since,))
        if len(rows) <= max_points:
            return rows
        step = len(rows) / max_points
        return [rows[int(i * step)] for i in range(max_points)]

    def prune(self, retention_days: int):
        cutoff = time.time() - retention_days * 86400
        self.x("DELETE FROM gpu_samples WHERE ts<?", (cutoff,))
        self.x("DELETE FROM logs WHERE ts<?", (cutoff,))


def _decode(r: dict) -> dict:
    for k, empty in (("metrics", {}), ("process_match", [])):
        try:
            r[k] = json.loads(r.get(k) or json.dumps(empty))
        except ValueError:
            r[k] = empty
    r["kind"] = r.get("kind") or "job"
    r["supports_resume"] = bool(r.get("supports_resume"))
    r["stop_requested"] = int(r.get("stop_requested") or 0)
    return r
