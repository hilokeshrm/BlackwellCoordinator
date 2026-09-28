"""Static project discovery for `activate`: entry point, framework, resume support, config hints."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".blackwell", "wandb", "outputs",
             "checkpoints", ".cursor", ".idea", ".vscode", "dist", "build"}
ENTRY_NAMES = ["train.py", "finetune.py", "fine_tune.py", "pretrain.py", "main.py", "run.py", "benchmark.py",
               "bench.py", "eval.py", "train.sh", "run.sh"]
FRAMEWORKS = [
    ("lightning", re.compile(r"import (pytorch_)?lightning|from (pytorch_)?lightning")),
    ("huggingface", re.compile(r"from transformers import|Trainer\(|SFTTrainer|from trl import|accelerate")),
    ("pytorch", re.compile(r"import torch")),
    ("jax", re.compile(r"import jax|import flax")),
    ("tensorflow", re.compile(r"import tensorflow|from tensorflow")),
]
RESUME_HINTS = re.compile(r"resume_from_checkpoint|--resume|load_state_dict|ckpt_path|save_pretrained|torch\.save|"
                          r"ModelCheckpoint|save_state|checkpoint", re.I)
SDK_HINT = re.compile(r"from blackwell_sdk import|import blackwell_sdk")
EPOCH_KEYS = ("epochs", "num_train_epochs", "max_epochs", "n_epochs", "num_epochs")
STEP_KEYS = ("max_steps", "total_steps", "num_steps", "iterations")


@dataclass
class ScanResult:
    root: Path
    name: str
    entry_file: str | None = None
    entry_command: str | None = None
    candidates: list[str] = field(default_factory=list)
    framework: str = "unknown"
    has_resume_logic: bool = False
    sdk_integrated: bool = False
    total_epochs: int | None = None
    total_steps: int | None = None
    config_file: str | None = None
    has_main_guard: bool = False
    python_files: int = 0
    readme: str = ""
    notes: list[str] = field(default_factory=list)


def _py_files(root: Path, limit=400):
    out = []
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1]) or p.name == "blackwell_sdk.py":
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _read(p: Path, n=200_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:n]
    except OSError:
        return ""


def _config_hints(root: Path, res: ScanResult):
    for pat in ("config.json", "config.yaml", "config.yml", "*.json", "*.yaml", "*.yml", "configs/*.json",
                "configs/*.yaml", "configs/*.yml"):
        for f in sorted(root.glob(pat)):
            if f.name.startswith(".") or f.stat().st_size > 200_000 or f.name in ("package.json", "tsconfig.json"):
                continue
            text = _read(f)
            found = False
            for keys, attr in ((EPOCH_KEYS, "total_epochs"), (STEP_KEYS, "total_steps")):
                for k in keys:
                    m = re.search(rf'["\']?{k}["\']?\s*[:=]\s*(\d+)', text)
                    if m and getattr(res, attr) is None:
                        setattr(res, attr, int(m.group(1)))
                        found = True
            if found and not res.config_file:
                res.config_file = str(f.relative_to(root)).replace("\\", "/")
            if res.total_epochs or res.total_steps:
                return


def _python(root: Path) -> str:
    """The project's own virtualenv interpreter if it has one; the daemon's PATH may point elsewhere."""
    for venv in (".venv", "venv", "env"):
        for exe in ("Scripts/python.exe", "bin/python"):
            if (root / venv / exe).exists():
                return f'"{root / venv / exe}"' if " " in str(root) else str(root / venv / exe)
    return "python"


def scan(root: Path, entry_override: str | None = None) -> ScanResult:
    root = root.resolve()
    res = ScanResult(root=root, name=root.name)
    files = _py_files(root)
    res.python_files = len(files)
    for n in ENTRY_NAMES:
        for p in [root / n, root / "src" / n, root / "scripts" / n]:
            if p.exists():
                res.candidates.append(str(p.relative_to(root)).replace("\\", "/"))
    if not res.candidates:
        for p in files:
            if "__main__" in _read(p, 50_000) and re.search(r"train|fit|epoch", _read(p, 50_000), re.I):
                res.candidates.append(str(p.relative_to(root)).replace("\\", "/"))
    all_text = "\n".join(_read(p, 60_000) for p in files[:120])
    for name, rx in FRAMEWORKS:
        if rx.search(all_text):
            res.framework = name
            break
    else:
        res.framework = "custom_script" if files else "unknown"
    res.sdk_integrated = bool(SDK_HINT.search(all_text))
    _config_hints(root, res)

    if res.candidates:
        res.entry_file = res.candidates[0]
        src = _read(root / res.entry_file)
        res.has_resume_logic = bool(RESUME_HINTS.search(src))
        res.has_main_guard = "__main__" in src
        if res.total_epochs is None:
            m = re.search(r"\b(EPOCHS|NUM_EPOCHS|epochs)\s*=\s*(\d+)", src)
            if m:
                res.total_epochs = int(m.group(2))
        if res.entry_file.endswith(".sh"):
            res.entry_command = f"bash {res.entry_file}"
        else:
            flag = ""
            if res.config_file and "--config" in src:
                flag = f" --config {res.config_file}"
            res.entry_command = f"{_python(root)} {res.entry_file}{flag}"
    if entry_override:
        res.entry_command = entry_override
        m = re.search(r"([\w./\\-]+\.(py|sh))", entry_override)
        if m:
            res.entry_file = m.group(1).replace("\\", "/")
    for r in ("README.md", "readme.md", "README.txt"):
        if (root / r).exists():
            res.readme = _read(root / r, 3000)
            break
    if not res.entry_command:
        res.notes.append("No training entry point detected — pass entry_command to activate.")
    if (root / "requirements.txt").exists() and not res.framework.startswith(("py", "hug", "light")):
        req = _read(root / "requirements.txt")
        if "torch" in req:
            res.framework = "pytorch"
    return res


COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/[\w./-]*)?")


def _walk(root: Path, depth: int = 3):
    stack = [(root, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if lvl < depth and e.name not in SKIP_DIRS and not e.name.startswith("."):
                    stack.append((e, lvl + 1))
            else:
                yield e


def service_hints(root: Path) -> dict:
    """Signals that a folder is a deployed, always-on service rather than a training job."""
    files = list(_walk(root))
    compose = [f for f in files if f.name in COMPOSE_NAMES]
    readmes = [f for f in files if f.name.lower() in ("readme.md", "readme.txt")][:5]
    text = "\n".join(_read(f, 60_000) for f in readmes)
    urls = URL_RE.findall(text)
    health = next((u for u in urls if "health" in u), None)
    envs = [f for f in files if f.name.startswith(".env") or f.name in COMPOSE_NAMES][:20]
    env_text = "\n".join(_read(f, 60_000) for f in envs)
    uses_ollama = bool(re.search(r"OLLAMA|:11434", env_text + text))
    trains = any((root / n).exists() for n in ("train.py", "finetune.py", "fine_tune.py", "pretrain.py"))
    return {
        "compose_files": [str(f.relative_to(root)).replace("\\", "/") for f in compose][:5],
        "urls": list(dict.fromkeys(urls))[:8],
        "health_url": health or (urls[0] if urls else None),
        "uses_ollama": uses_ollama,
        "looks_like_service": bool(compose) and not trains,
    }


def as_dict(r: ScanResult) -> dict:
    d = dict(r.__dict__)
    d["root"] = str(r.root)
    d.pop("readme", None)
    return d


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
