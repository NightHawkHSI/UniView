"""Keeping the app responsive: system stats for the title bar (RAM, CPU, GPU, VRAM) and the throttle
the background workers check so the window gets the computer first when it's struggling. No Qt here.

Stats come straight from Windows (psapi/kernel32 and the PDH counters Task Manager uses), so there's no
extra dependency; on other systems they're simply missing (None).
"""

import ctypes
import os
import sys
import threading
import time

from uniview.constants import log

_WIN = sys.platform == "win32"


# ---- system stats (Windows)
class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _ProcMem(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


class _CounterValue(ctypes.Structure):  # PDH_FMT_COUNTERVALUE with the double member of its union
    _fields_ = [("CStatus", ctypes.c_ulong), ("doubleValue", ctypes.c_double)]


class _CounterItem(ctypes.Structure):  # PDH_FMT_COUNTERVALUE_ITEM_W
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _CounterValue)]


_PDH_FMT_DOUBLE_NOCAP = 0x00000200 | 0x00008000
_PDH_MORE_DATA = 0x800007D2


class _GpuCounters:
    """Overall GPU use (the busiest engine type, like Task Manager) and this process's dedicated VRAM."""

    def __init__(self):
        self.pdh = ctypes.WinDLL("pdh")
        self.query = ctypes.c_void_p()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)):
            raise OSError("PdhOpenQuery failed")
        self.util = self._add(r"\GPU Engine(*)\Utilization Percentage")
        self.vram = self._add(r"\GPU Process Memory(*)\Dedicated Usage")
        self.pid_tag = f"pid_{os.getpid()}_"
        self.pdh.PdhCollectQueryData(self.query)  # utilization is a rate: needs a first sample

    def _add(self, path):
        counter = ctypes.c_void_p()
        if self.pdh.PdhAddEnglishCounterW(self.query, path, None, ctypes.byref(counter)):
            raise OSError(f"No performance counter {path}")
        return counter

    def _values(self, counter):
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        status = self.pdh.PdhGetFormattedCounterArrayW(counter, _PDH_FMT_DOUBLE_NOCAP, ctypes.byref(size),
                                                       ctypes.byref(count), None)
        if status & 0xFFFFFFFF != _PDH_MORE_DATA or not size.value:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if self.pdh.PdhGetFormattedCounterArrayW(counter, _PDH_FMT_DOUBLE_NOCAP, ctypes.byref(size),
                                                 ctypes.byref(count), buf):
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_CounterItem))
        return [(items[i].szName or "", items[i].FmtValue.doubleValue) for i in range(count.value)
                if items[i].FmtValue.CStatus in (0, 1)]  # PDH_CSTATUS_VALID_DATA / NEW_DATA

    def sample(self):
        """(gpu percent, this app's VRAM bytes); None for whichever isn't available."""
        if self.pdh.PdhCollectQueryData(self.query):
            return None, None
        engines = {}  # (adapter luid, engine type) -> summed use over all processes
        for name, value in self._values(self.util):
            luid = name.split("_luid_")[-1].split("_phys_")[0]
            kind = name.rsplit("engtype_", 1)[-1]
            engines[(luid, kind)] = engines.get((luid, kind), 0.0) + value
        gpu = min(100.0, max(engines.values())) if engines else None
        vram = [v for name, v in self._values(self.vram) if name.startswith(self.pid_tag)]
        return gpu, (sum(vram) if vram else None)


class SystemStats:
    """Samples the stats on its own thread every `interval` seconds; latest() reads the newest set."""

    def __init__(self, interval=2.0):
        self.interval = interval
        self._latest = {}
        self._stop = threading.Event()
        if _WIN:
            threading.Thread(target=self._run, daemon=True, name="system-stats").start()

    def latest(self):
        """{"app_ram", "sys_ram_pct", "sys_ram_total", "cpu", "gpu", "vram"} - any may be missing."""
        return dict(self._latest)

    def stop(self):
        self._stop.set()

    def _run(self):
        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        proc = ctypes.c_void_p(k32.GetCurrentProcess())
        try:
            gpu = _GpuCounters()
        except Exception as e:  # counters missing (old Windows, disabled perf counters, VM)
            log.debug("GPU stats unavailable: %s", e)
            gpu = None
        cpus = os.cpu_count() or 1
        last_cpu, last_wall = None, None
        while not self._stop.wait(self.interval if last_cpu is not None else 0.5):
            stats = {}
            try:
                mem = _MemStatus()
                mem.dwLength = ctypes.sizeof(mem)
                if k32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                    stats["sys_ram_pct"] = int(mem.dwMemoryLoad)
                    stats["sys_ram_total"] = int(mem.ullTotalPhys)
                pm = _ProcMem()
                pm.cb = ctypes.sizeof(pm)
                if psapi.GetProcessMemoryInfo(proc, ctypes.byref(pm), pm.cb):
                    stats["app_ram"] = int(pm.WorkingSetSize)
                times = [ctypes.c_ulonglong() for _ in range(4)]  # creation, exit, kernel, user (100 ns)
                if k32.GetProcessTimes(proc, *[ctypes.byref(t) for t in times]):
                    used, wall = (times[2].value + times[3].value) / 1e7, time.monotonic()
                    if last_cpu is not None and wall > last_wall:
                        stats["cpu"] = max(0.0, min(100.0, 100.0 * (used - last_cpu) / (wall - last_wall) / cpus))
                    last_cpu, last_wall = used, wall
                if gpu is not None:
                    g, v = gpu.sample()
                    if g is not None:
                        stats["gpu"] = g
                    if v is not None:
                        stats["vram"] = v
            except Exception as e:
                log.debug("System stats failed: %s", e)
            self._latest = stats


def fmt_gb(n):
    return f"{n / 2**30:.1f} GB" if n >= 2**30 * 0.95 else f"{n / 2**20:.0f} MB"


def stats_text(stats):
    """One line for the title bar, e.g. 'RAM 1.8 GB (system 71%) · CPU 14% · GPU 23% · VRAM 640 MB'."""
    parts = []
    if "app_ram" in stats:
        ram = "RAM " + fmt_gb(stats["app_ram"])
        if "sys_ram_pct" in stats:
            ram += f" (system {stats['sys_ram_pct']}%)"
        parts.append(ram)
    if "cpu" in stats:
        parts.append(f"CPU {stats['cpu']:.0f}%")
    if "gpu" in stats:
        parts.append(f"GPU {stats['gpu']:.0f}%")
    if "vram" in stats:
        parts.append("VRAM " + fmt_gb(stats["vram"]))
    return " · ".join(parts)


# ---- throttle
class Throttle:
    """How hard the background workers (thumbnails, stats) may push. The window reports how late its
    event loop runs and how full memory is; workers call pause() between jobs.

    Levels: 0 = full speed, 1 = easing off (short sleeps), 2 = struggling (long sleeps).
    While the user is clicking through assets (user_active) workers wait, so the preview gets the
    session lock and the CPU first.
    """

    LAG_HIGH_MS = 250     # one event-loop hiccup this long raises the level
    LAG_SEVERE_MS = 1500  # this long (a near-freeze) goes straight to level 2
    CALM_SECONDS = 4.0    # this long without hiccups lowers the level by one
    MEM_EASE, MEM_SEVERE = 85, 93  # system memory load percent
    SLEEPS = (0.0, 0.04, 0.3)

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._lag_level = 0
        self._mem_level = 0
        self._calm_since = clock()
        self._active_until = 0.0

    @property
    def level(self):
        with self._lock:
            return max(self._lag_level, self._mem_level)

    def reason(self):
        """Why the workers are slowed, for the status bar ('' at full speed)."""
        with self._lock:
            if self._mem_level:
                return "memory is almost full"
            if self._lag_level:
                return "the window was lagging"
            return ""

    def report_lag(self, lag_ms):
        """How late the window's heartbeat timer fired. Returns True if the level changed."""
        with self._lock:
            before, now = self._lag_level, self._clock()
            if lag_ms >= self.LAG_SEVERE_MS:
                self._lag_level, self._calm_since = 2, now
            elif lag_ms >= self.LAG_HIGH_MS:
                self._lag_level, self._calm_since = max(1, self._lag_level), now
            elif self._lag_level and now - self._calm_since >= self.CALM_SECONDS:
                self._lag_level -= 1
                self._calm_since = now
            return self._lag_level != before

    def report_memory(self, load_pct):
        """System memory load percent. Returns True if the level changed."""
        with self._lock:
            before = self._mem_level
            self._mem_level = 2 if load_pct >= self.MEM_SEVERE else 1 if load_pct >= self.MEM_EASE else 0
            return self._mem_level != before

    def user_active(self, seconds=0.8):
        """The user just did something that needs the session (selected an asset): workers wait."""
        with self._lock:
            self._active_until = max(self._active_until, self._clock() + seconds)

    def pause(self, sleep=time.sleep):
        """Called by a worker between jobs: waits while the user is active, then sleeps per level."""
        while True:
            with self._lock:
                wait = self._active_until - self._clock()
            if wait <= 0:
                break
            sleep(min(wait, 0.1))
        sleep(self.SLEEPS[self.level])


THROTTLE = Throttle()  # shared by the window and the workers


class FreezeWatch:
    """Logs what the UI thread is doing when it stops answering for `limit` seconds (once per freeze),
    so a 'Not Responding' in viewer.log says which code caused it. The window calls beat() from a timer."""

    def __init__(self, limit=3.0):
        self.limit = limit
        self._ui_thread = threading.get_ident()  # created on the UI thread
        self._last = time.monotonic()
        self.paused = False  # while a game loads (the window doesn't beat then)
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="freeze-watch").start()

    def beat(self):
        self._last = time.monotonic()

    def stop(self):
        self._stop.set()

    def _run(self):
        import traceback
        reported = False
        while not self._stop.wait(0.5):
            stalled = time.monotonic() - self._last
            if self.paused or stalled < self.limit:
                reported = False
                continue
            if reported:
                continue
            reported = True
            frame = sys._current_frames().get(self._ui_thread)
            stack = "".join(traceback.format_stack(frame)[-12:]) if frame is not None else "(unknown)\n"
            log.warning("The window hasn't responded for %.0f s - it's busy in:\n%s", stalled, stack.rstrip())
