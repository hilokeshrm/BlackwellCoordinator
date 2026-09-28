import shutil
import subprocess
import time
from dataclasses import dataclass


@dataclass
class GpuSample:
    ts: float
    index: int
    name: str
    util: float
    mem_used_mb: float
    mem_total_mb: float
    temp_c: float
    power_w: float

    def as_dict(self):
        return {
            "ts": self.ts,
            "index": self.index,
            "name": self.name,
            "utilization_percent": self.util,
            "memory_used_mb": self.mem_used_mb,
            "memory_total_mb": self.mem_total_mb,
            "memory_percent": round(100 * self.mem_used_mb / self.mem_total_mb, 1) if self.mem_total_mb else 0,
            "temperature_c": self.temp_c,
            "power_w": self.power_w,
        }


class NvidiaSmiProvider:
    FIELDS = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"

    def __init__(self):
        self.exe = shutil.which("nvidia-smi")
        if not self.exe:
            raise RuntimeError("nvidia-smi not found")

    def sample(self) -> list[GpuSample]:
        out = subprocess.run(
            [self.exe, f"--query-gpu={self.FIELDS}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        now = time.time()
        res = []
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            num = lambda v: float(v) if v.replace(".", "", 1).isdigit() else 0.0
            res.append(GpuSample(now, int(p[0]), p[1], num(p[2]), num(p[3]), num(p[4]), num(p[5]), num(p[6])))
        return res


class MockGpuProvider:
    """Test double only. Util follows running jobs' declared GPU estimate, or a forced value."""

    def __init__(self):
        self.forced_util: float | None = None
        self.job_load: float = 0.0

    def sample(self) -> list[GpuSample]:
        util = self.forced_util if self.forced_util is not None else min(100.0, 3.0 + self.job_load)
        mem = 2048 + 900 * util
        return [GpuSample(time.time(), 0, "Mock Blackwell (simulated)", util, mem, 97887, 40 + util * 0.4, 60 + util * 5)]


def make_provider(mock: bool):
    return MockGpuProvider() if mock else NvidiaSmiProvider()
