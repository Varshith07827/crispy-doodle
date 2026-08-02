"""Port of WinSpark.Infrastructure.Services.ProcessMetricsService using psutil instead of System.Diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import psutil


@dataclass(slots=True)
class ProcessMetrics:
    memory_bytes: int = 0
    cpu_percent: float = 0.0
    start_time_utc: Optional[datetime] = None


class ProcessMetricsService:
    """Caches psutil.Process handles per PID, same intent as the .NET version's per-PID sampling."""

    def __init__(self) -> None:
        self._processes: dict[int, psutil.Process] = {}

    def get_metrics(self, pid: int) -> ProcessMetrics:
        proc = self._processes.get(pid)
        if proc is None or not proc.is_running():
            try:
                proc = psutil.Process(pid)
                self._processes[pid] = proc
            except psutil.NoSuchProcess:
                return ProcessMetrics()

        try:
            with proc.oneshot():
                memory_bytes = proc.memory_info().rss
                cpu_percent = proc.cpu_percent(interval=None)
                start_time_utc = datetime.fromtimestamp(proc.create_time(), tz=timezone.utc)
            return ProcessMetrics(memory_bytes=memory_bytes, cpu_percent=cpu_percent, start_time_utc=start_time_utc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return ProcessMetrics()

    def prune_stale_samples(self, live_pids: set[int]) -> None:
        for pid in list(self._processes.keys()):
            if pid not in live_pids:
                del self._processes[pid]
