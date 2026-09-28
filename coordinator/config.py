import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("BLACKWELL_HOME", Path.home() / ".blackwell"))


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 9477
    db_path: str = str(HOME / "coordinator.db")
    api_token: str = ""  # empty = no token required for mutating calls

    idle_threshold_percent: float = 10.0
    idle_grace_seconds: int = 60
    gpu_poll_seconds: float = 2.0
    scheduler_tick_seconds: float = 5.0
    heartbeat_timeout_seconds: int = 300
    stop_grace_seconds: int = 120

    night_start_hour: int = 22
    night_end_hour: int = 7
    long_job_minutes: int = 8 * 60
    gap_buffer_minutes: int = 15
    preempt_for_short_jobs: bool = True
    default_gpu_percent: float = 90.0
    service_check_seconds: float = 20.0
    exclude_service_load: bool = True  # deployed services' GPU load doesn't count toward the idle threshold

    mock_gpu: bool = False  # tests only; the daemon always reads the real GPU
    decider: str = "auto"  # auto (jev -> ollama -> heuristic) | jev | ollama | heuristic
    jev_url: str = "http://127.0.0.1:8791"
    jev_timeout_seconds: float = 5.0
    jev_min_probability: float = 0.55
    preempt_recheck_seconds: int = 600
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    log_retention_days: int = 30
    extra: dict = field(default_factory=dict)


ENV_MAP = {
    "BLACKWELL_PORT": ("port", int),
    "BLACKWELL_HOST": ("host", str),
    "BLACKWELL_DB": ("db_path", str),
    "BLACKWELL_API_TOKEN": ("api_token", str),
    "BLACKWELL_IDLE_GRACE": ("idle_grace_seconds", int),
    "BLACKWELL_THRESHOLD": ("idle_threshold_percent", float),
    "BLACKWELL_TICK": ("scheduler_tick_seconds", float),
    "BLACKWELL_DECIDER": ("decider", str),
    "BLACKWELL_OLLAMA_MODEL": ("ollama_model", str),
    "JEV_URL": ("jev_url", str),
    "JEV_MIN_PROBABILITY": ("jev_min_probability", float),
}


def load_settings() -> Settings:
    s = Settings()
    cfg = HOME / "config.json"
    if cfg.exists():
        for k, v in json.loads(cfg.read_text()).items():
            if hasattr(s, k):
                setattr(s, k, v)
    for env, (attr, cast) in ENV_MAP.items():
        if env in os.environ:
            setattr(s, attr, cast(os.environ[env]))
    Path(s.db_path).parent.mkdir(parents=True, exist_ok=True)
    return s


def public_settings(s: Settings) -> dict:
    d = asdict(s)
    d.pop("api_token", None)
    d.pop("db_path", None)
    return d
