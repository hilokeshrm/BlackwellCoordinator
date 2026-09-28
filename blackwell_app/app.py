"""Blackwell Coordinator desktop app: native window + system-tray icon.

The coordinator daemon (scheduled task) does the scheduling; this app is the desktop front end. It keeps running
in the tray when the window is closed, shows GPU load in the tray tooltip, and pops Windows notifications when
jobs start/finish/fail or a deployed service goes down.
"""
import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import pystray
import webview

from coordinator.config import HOME
from .icon import ensure_ico, render

TITLE = "Blackwell Coordinator"
SHOW_EVENT = "Local\\BlackwellCoordinatorShow"
ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("BLACKWELL_DASHBOARD_URL", "http://127.0.0.1:9477")
ICO = HOME / "app.ico"


class App:
    def __init__(self, start_hidden: bool):
        self.window = None
        self.quitting = False
        self.start_hidden = start_hidden
        self.state: dict = {}
        self.seen: dict[str, str] = {}
        self.tray = pystray.Icon("BlackwellCoordinator", render(64), TITLE, menu=self._menu())

    # ---------- coordinator ----------
    def healthy(self) -> bool:
        try:
            return httpx.get(f"{URL}/api/v1/health", timeout=2).status_code == 200
        except Exception:
            return False

    def ensure_daemon(self):
        if self.healthy():
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        started = subprocess.run(["schtasks", "/Run", "/TN", "BlackwellCoordinator"], capture_output=True,
                                 creationflags=flags).returncode == 0
        if not started:
            subprocess.Popen([os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "conhost.exe"), "--headless",
                              sys.executable, "-m", "coordinator.main", "--log-file"], cwd=ROOT, creationflags=flags)
        for _ in range(40):
            if self.healthy():
                return
            time.sleep(0.5)

    # ---------- tray ----------
    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem("Open Blackwell Coordinator", self.show, default=True),
            pystray.MenuItem(lambda _: self._summary(), None, enabled=False),
            pystray.MenuItem("Open in browser", lambda: webbrowser.open(URL + "/")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", self.toggle_autostart, checked=lambda _: self.autostart_enabled()),
            pystray.MenuItem("Quit app (coordinator keeps running)", self.quit),
        )

    def _summary(self) -> str:
        s = self.state
        if not s:
            return "Connecting to the coordinator…"
        g = s.get("gpu") or {}
        run = next((p for p in s.get("projects", []) if p["status"] in ("running", "stopping")), None)
        tail = f" · {run['name']} {run['percent']:.0f}%" if run else (" · free" if s.get("can_start_new_job") else " · busy")
        return f"GPU {g.get('utilization_percent', 0):.0f}% · {g.get('memory_used_mb', 0) / 1024:.0f}/{g.get('memory_total_mb', 0) / 1024:.0f} GB{tail}"

    def poll(self):
        while not self.quitting:
            try:
                self.state = httpx.get(f"{URL}/api/v1/state", timeout=5).json()
                self.tray.title = f"{TITLE}\n{self._summary()}"[:127]
                self._notify_changes()
                self._update_icon()
            except Exception:
                self.state = {}
                self.tray.title = f"{TITLE}\nCoordinator not reachable"
            self.tray.update_menu()
            time.sleep(4)

    def _update_icon(self):
        down = any(v["svc_state"] == "down" for v in self.state.get("services", []))
        running = any(p["status"] in ("running", "stopping") for p in self.state.get("projects", []))
        key = "down" if down else "run" if running else "idle"
        if getattr(self, "_icon_key", None) != key:
            self._icon_key = key
            self.tray.icon = render(64, dot=(255, 77, 109) if down else (255, 138, 61) if running else None)

    def _notify_changes(self):
        msgs = []
        for p in self.state.get("projects", []):
            old, new = self.seen.get(p["id"]), p["status"]
            if old and old != new:
                if new == "running":
                    msgs.append((f"{p['name']} started", f"{p['owner']} · finishes around {self._when(p.get('projected_finish'))}"))
                elif new == "completed":
                    msgs.append((f"{p['name']} finished", f"{p['owner']}'s job is done."))
                elif new == "failed":
                    msgs.append((f"{p['name']} failed", p.get("message") or "Check the logs."))
                elif new in ("paused", "queued") and old in ("running", "stopping"):
                    msgs.append((f"{p['name']} paused", p.get("message") or "Saved a checkpoint."))
            self.seen[p["id"]] = new
        for v in self.state.get("services", []):
            key, old, new = "svc" + v["id"], self.seen.get("svc" + v["id"]), v["svc_state"]
            if old and old != new and new in ("up", "down"):
                msgs.append((f"{v['name']} is {'back up' if new == 'up' else 'down'}", v.get("svc_detail") or ""))
            self.seen[key] = new
        for title, body in msgs[:3]:
            try:
                self.tray.notify(body or " ", title)
            except Exception:
                pass

    @staticmethod
    def _when(ts):
        return time.strftime("%a %H:%M", time.localtime(ts)) if ts else "—"

    # ---------- autostart ----------
    @staticmethod
    def _startup_lnk() -> Path:
        return Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup\Blackwell Coordinator.lnk"

    def autostart_enabled(self) -> bool:
        return self._startup_lnk().exists()

    def toggle_autostart(self):
        lnk = self._startup_lnk()
        if lnk.exists():
            lnk.unlink()
        else:
            make_shortcut(lnk, tray=True)

    # ---------- window ----------
    def show(self, *_):
        if self.window:
            self.window.show()
            try:
                self.window.restore()
            except Exception:
                pass
            _foreground(TITLE)

    def on_closing(self):
        if self.quitting:
            return True
        self.window.hide()
        if not self.seen.get("_hint"):
            self.seen["_hint"] = "1"
            try:
                self.tray.notify("Still running in the tray. Right-click the icon to quit.", TITLE)
            except Exception:
                pass
        return False

    def on_shown(self):
        _dark_titlebar(TITLE)

    def quit(self, *_):
        self.quitting = True
        self.tray.stop()
        if self.window:
            self.window.destroy()

    def _listen_show(self):
        """A second launch (Start menu, desktop shortcut) signals this event instead of opening another window."""
        k32 = ctypes.windll.kernel32
        ev = k32.CreateEventW(None, False, False, SHOW_EVENT)
        while not self.quitting:
            if k32.WaitForSingleObject(ev, 1000) == 0:
                self.show()

    def run(self):
        ensure_ico(ICO)
        threading.Thread(target=self._listen_show, daemon=True).start()
        threading.Thread(target=self.ensure_daemon, daemon=True).start()
        self.tray.run_detached()
        threading.Thread(target=self.poll, daemon=True).start()
        self.window = webview.create_window(TITLE, URL + "/?app=1", width=1480, height=940, min_size=(420, 560),
                                            background_color="#0B0E13", hidden=self.start_hidden, text_select=True)
        self.window.events.closing += self.on_closing
        self.window.events.shown += self.on_shown
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(HOME / "webview"), icon=str(ICO))


def _dark_titlebar(title: str):
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            val = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
            color = ctypes.c_int(0x00130E0B)  # COLORREF 0x00BBGGRR for #0B0E13
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
    except Exception:
        pass


def _foreground(title: str) -> bool:
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


LAUNCHER = """Set sh = CreateObject("WScript.Shell")
args = ""
For Each a In WScript.Arguments
  args = args & " " & a
Next
sh.CurrentDirectory = "{root}"
sh.Run \"\"\"{py}\"\" -m blackwell_app.app\" & args, 0, False
"""


def make_shortcut(lnk: Path, tray: bool = False):
    """Windows Application Control blocks the venv's pythonw.exe, and a headless console breaks WebView2
    rendering, so start python.exe through a tiny VBScript that hides its console window."""
    ensure_ico(ICO)
    vbs = HOME / "launch-app.vbs"
    vbs.write_text(LAUNCHER.format(root=ROOT, py=sys.executable))
    args = f'"{vbs}"' + (" --tray" if tray else "")
    target = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "wscript.exe")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{target}';"
          f"$s.Arguments='{args.replace(chr(39), chr(39) * 2)}';$s.WorkingDirectory='{ROOT}';$s.IconLocation='{ICO},0';"
          f"$s.Description='{TITLE}';$s.Save()")
    lnk.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main():
    ap = argparse.ArgumentParser(description="Blackwell Coordinator desktop app")
    ap.add_argument("--tray", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--install-shortcuts", action="store_true", help="add Start menu + Startup shortcuts and exit")
    a = ap.parse_args()
    if a.install_shortcuts:
        programs = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs"
        make_shortcut(programs / "Blackwell Coordinator.lnk")
        make_shortcut(App._startup_lnk(), tray=True)
        print("Shortcuts created: Start menu and Startup (tray).")
        return
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\BlackwellCoordinatorApp")
    if ctypes.windll.kernel32.GetLastError() == 183:  # already running: ask it to show its window
        if not a.tray:
            k32 = ctypes.windll.kernel32
            ev = k32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
            if ev:
                k32.SetEvent(ev)
                k32.CloseHandle(ev)
        return
    App(start_hidden=a.tray).run()
    del mutex


if __name__ == "__main__":
    main()
