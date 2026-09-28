"""Per-process GPU memory and utilisation. On Windows (WDDM) nvidia-smi reports N/A per process, so this reads
the same performance counters Task Manager uses via the PDH API."""
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

import psutil

INST = re.compile(r"pid_(\d+)_")


@dataclass
class ProcUsage:
    pid: int
    mem_mb: float = 0.0
    util: float = 0.0


class _Pdh:
    PDH_FMT_DOUBLE = 0x00000200
    PDH_FMT_NOCAP100 = 0x00008000
    PDH_MORE_DATA = 0x800007D2

    def __init__(self):
        import ctypes as C
        from ctypes import wintypes as W

        class FMT(C.Structure):
            _fields_ = [("CStatus", W.DWORD), ("doubleValue", C.c_double)]

        class ITEM(C.Structure):
            _fields_ = [("szName", W.LPWSTR), ("FmtValue", FMT)]

        self.C, self.W, self.ITEM = C, W, ITEM
        self.pdh = C.WinDLL("pdh.dll")
        self.pdh.PdhGetFormattedCounterArrayW.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.c_void_p]
        self.pdh.PdhGetFormattedCounterArrayW.restype = W.DWORD
        self.query = W.HANDLE()
        if self.pdh.PdhOpenQueryW(None, 0, C.byref(self.query)):
            raise OSError("PdhOpenQuery failed")
        self.mem = self._add(r"\GPU Process Memory(*)\Dedicated Usage")
        self.util = self._add(r"\GPU Engine(*)\Utilization Percentage")
        self.pdh.PdhCollectQueryData(self.query)

    def _add(self, path):
        h = self.W.HANDLE()
        if self.pdh.PdhAddEnglishCounterW(self.query, path, 0, self.C.byref(h)):
            raise OSError(f"cannot add counter {path}")
        return h

    def _read(self, counter, fmt):
        C, W = self.C, self.W
        size, count = W.DWORD(0), W.DWORD(0)
        rc = self.pdh.PdhGetFormattedCounterArrayW(counter, fmt, C.byref(size), C.byref(count), None)
        if rc != self.PDH_MORE_DATA or not size.value:
            return []
        buf = (C.c_byte * size.value)()
        if self.pdh.PdhGetFormattedCounterArrayW(counter, fmt, C.byref(size), C.byref(count), buf):
            return []
        items = C.cast(buf, C.POINTER(self.ITEM))
        return [(items[i].szName, items[i].FmtValue.doubleValue) for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)]

    def sample(self) -> dict[int, ProcUsage]:
        self.pdh.PdhCollectQueryData(self.query)
        out: dict[int, ProcUsage] = {}
        for name, val in self._read(self.mem, self.PDH_FMT_DOUBLE):
            m = INST.search(name or "")
            if m:
                pid = int(m.group(1))
                out.setdefault(pid, ProcUsage(pid)).mem_mb += val / (1024 * 1024)
        for name, val in self._read(self.util, self.PDH_FMT_DOUBLE | self.PDH_FMT_NOCAP100):
            m = INST.search(name or "")
            if m and val > 0:
                pid = int(m.group(1))
                u = out.setdefault(pid, ProcUsage(pid))
                u.util = max(u.util, val)  # busiest engine, like Task Manager's per-process GPU column
        return out


def _smi_sample() -> dict[int, ProcUsage]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    txt = subprocess.run([exe, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=10).stdout
    out = {}
    for line in txt.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 2 and p[0].isdigit():
            out[int(p[0])] = ProcUsage(int(p[0]), float(p[1]) if p[1].replace(".", "", 1).isdigit() else 0.0)
    return out


class ProcSampler:
    def __init__(self):
        self.pdh = None
        if sys.platform == "win32":
            try:
                self.pdh = _Pdh()
            except Exception:
                self.pdh = None

    def sample(self) -> list[dict]:
        raw = self.pdh.sample() if self.pdh else _smi_sample()
        res = []
        for pid, u in raw.items():
            if u.mem_mb < 16 and u.util < 0.5:
                continue
            try:
                p = psutil.Process(pid)
                name = p.name()
                try:
                    cmd = " ".join(p.cmdline())[:400]
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    cmd = ""
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name, cmd = f"pid {pid}", ""
            res.append({"pid": pid, "name": name, "cmd": cmd, "mem_mb": round(u.mem_mb, 1), "util": round(u.util, 1)})
        return sorted(res, key=lambda r: -r["mem_mb"])


def descendants(pid: int) -> set[int]:
    try:
        p = psutil.Process(pid)
        return {pid, *(c.pid for c in p.children(recursive=True))}
    except psutil.Error:
        return {pid}
