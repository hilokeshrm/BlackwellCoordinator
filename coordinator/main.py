import argparse
import sys

import uvicorn

from .api import create_app
from .config import HOME, load_settings


def run():
    ap = argparse.ArgumentParser(description="BlackwellCoordinator daemon")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--local-only", action="store_true", help="bind 127.0.0.1 instead of the LAN")
    ap.add_argument("--log-file", action="store_true", help="write output to ~/.blackwell/coordinator.log")
    a = ap.parse_args()
    s = load_settings()
    if a.log_file or sys.stdout is None:
        log = open(HOME / "coordinator.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    if a.port:
        s.port = a.port
    if a.host:
        s.host = a.host
    if a.local_only:
        s.host = "127.0.0.1"
    print(f"BlackwellCoordinator -> http://{'127.0.0.1' if s.host == '0.0.0.0' else s.host}:{s.port}/  (db {s.db_path})")
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    run()
