"""Actual process, database and market-source observations for the QQ console."""

import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock


class QuantHealth:
    def __init__(self, *, database: Callable, source: Callable, engine: Callable):
        self.database, self.source, self.engine = database, source, engine
        self._lock = Lock()
        self._last_wall, self._last_cpu = time.monotonic(), time.process_time()

    def read(self) -> dict:
        try:
            database = "available" if self.database() else "unavailable"
        except Exception:
            database = "unavailable"
        try:
            source = self.source()
        except Exception:
            source = {}
        try:
            engine = self.engine()
        except Exception:
            engine = {"version": None, "entitlement_supported": False}
        rss_bytes = None
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss_bytes = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            pass
        with self._lock:
            wall, cpu = time.monotonic(), time.process_time()
            elapsed = wall - self._last_wall
            percent = 100 * (cpu - self._last_cpu) / elapsed if elapsed > 0 else 0.0
            self._last_wall, self._last_cpu = wall, cpu
        return {
            "database": database,
            "source": source,
            "engine": engine,
            "rss_bytes": rss_bytes,
            "cpu_seconds": round(cpu, 3),
            "cpu_percent": round(percent, 1),
        }
