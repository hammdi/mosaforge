"""Stdlib-only system metrics for Windows (Dell worker), Linux and macOS (Mac sampler).

Returns GB and percent. CPU percent is computed between two successive calls.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading

_prev_cpu: tuple[float, float] | None = None  # (idle, total)
_lock = threading.Lock()


def _cpu_times() -> tuple[float, float] | None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime  # noqa: E731
        return float(f(idle)), float(f(kernel) + f(user))  # kernel time includes idle
    if sys.platform.startswith("linux"):
        with open("/proc/stat") as fh:
            vals = [float(x) for x in fh.readline().split()[1:]]
        return vals[3] + vals[4], sum(vals)
    return None


def cpu_percent() -> float | None:
    """System-wide CPU busy % since the previous call (None on the first call or unsupported OS)."""
    global _prev_cpu
    if sys.platform == "darwin":  # no stdlib counters: sum of per-process %CPU (decayed average) / cores
        out = subprocess.run(["ps", "-A", "-o", "%cpu="], capture_output=True, text=True).stdout.split()
        return round(sum(float(x) for x in out) / (os.cpu_count() or 1), 1)
    with _lock:
        now = _cpu_times()
        if now is None:
            return None
        prev, _prev_cpu = _prev_cpu, now
        if prev is None or now[1] <= prev[1]:
            return None
        return round(100.0 * (1 - (now[0] - prev[0]) / (now[1] - prev[1])), 1)


def memory() -> dict[str, float]:
    """total_gb, used_gb, avail_gb, swap_used_gb (best effort per OS)."""
    gb = 1024**3
    if sys.platform == "win32":
        import ctypes

        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        m = MEMSTAT()
        m.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        commit_used = (m.ullTotalPageFile - m.ullAvailPageFile) - (m.ullTotalPhys - m.ullAvailPhys)
        return {"total_gb": round(m.ullTotalPhys / gb, 2), "used_gb": round((m.ullTotalPhys - m.ullAvailPhys) / gb, 2),
                "avail_gb": round(m.ullAvailPhys / gb, 2), "swap_used_gb": round(max(commit_used, 0) / gb, 2)}
    if sys.platform.startswith("linux"):
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = float(v.split()[0]) * 1024
        return {"total_gb": round(info["MemTotal"] / gb, 2), "used_gb": round((info["MemTotal"] - info["MemAvailable"]) / gb, 2),
                "avail_gb": round(info["MemAvailable"] / gb, 2), "swap_used_gb": round((info["SwapTotal"] - info["SwapFree"]) / gb, 2)}
    if sys.platform == "darwin":
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        page = int(re.search(r"page size of (\d+)", vm).group(1))  # type: ignore[union-attr]
        pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^(.+?):\s+(\d+)\.?$", vm, re.M)}
        # Activity Monitor's "Memory Used" = app (anonymous) + wired + compressed
        used = (pages.get("Anonymous pages", 0) - pages.get("Pages purgeable", 0) + pages.get("Pages wired down", 0)
                + pages.get("Pages occupied by compressor", 0)) * page
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
        swap_used = re.search(r"used = ([\d.]+)M", sw)
        return {"total_gb": round(total / gb, 2), "used_gb": round(used / gb, 2), "avail_gb": round((total - used) / gb, 2),
                "swap_used_gb": round(float(swap_used.group(1)) / 1024, 2) if swap_used else 0.0}
    return {}


def snapshot() -> dict[str, float | None]:
    return {**memory(), "cpu_pct": cpu_percent(), "os": sys.platform, "cpus": os.cpu_count()}
