"""Port of WinSpark.Infrastructure.Automation.StaAutomationThreadManager.

UI Automation (like most COM) is not reliably safe to call from arbitrary or
rotating threads. The .NET version dedicates a single background thread with
its COM apartment set to STA and marshals every UI Automation call onto it;
this does the same with a plain `threading.Thread` plus `pythoncom.CoInitialize()`
(STA is the default apartment when called with no arguments — the Python
equivalent of `ApartmentState.STA`).

Simplification vs. the .NET version: work items don't carry a cancellation
token. Asyncio callers can still cancel their own `await`, but a work item
already queued will run to completion on the STA thread regardless — same
as the .NET version once an item has been dequeued.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class AutomationThreadHealth:
    is_running: bool = False
    thread_id: int = 0
    pending_requests: int = 0
    total_requests_processed: int = 0
    failed_requests: int = 0
    last_request_utc: Optional[datetime] = None
    last_restart_utc: Optional[datetime] = None
    restart_count: int = 0
    last_error: Optional[str] = None


@dataclass
class _WorkItem:
    func: Callable[[], object]
    future: "concurrent.futures.Future"


class StaAutomationThreadManager:
    """Port of IAutomationThreadManager / StaAutomationThreadManager."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[Optional[_WorkItem]]" = queue.Queue()
        self._thread_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._disposed = False
        self._total_processed = 0
        self._failed_requests = 0
        self._last_error: Optional[str] = None
        self._last_request_utc: Optional[datetime] = None
        self._last_restart_utc: Optional[datetime] = None
        self._restart_count = 0
        self._start_thread()

    @property
    def is_healthy(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._disposed

    @property
    def pending_request_count(self) -> int:
        return self._queue.qsize()

    @property
    def last_restart_utc(self) -> Optional[datetime]:
        return self._last_restart_utc

    @property
    def restart_count(self) -> int:
        return self._restart_count

    async def invoke_async(self, work: Callable[[], T]) -> T:
        self._ensure_healthy()
        future: "concurrent.futures.Future" = concurrent.futures.Future()
        self._queue.put(_WorkItem(work, future))
        return await asyncio.wrap_future(future)

    def get_health(self) -> AutomationThreadHealth:
        return AutomationThreadHealth(
            is_running=self.is_healthy,
            thread_id=self._thread.ident or 0 if self._thread else 0,
            pending_requests=self.pending_request_count,
            total_requests_processed=self._total_processed,
            failed_requests=self._failed_requests,
            last_request_utc=self._last_request_utc,
            last_restart_utc=self._last_restart_utc,
            restart_count=self._restart_count,
            last_error=self._last_error,
        )

    def _ensure_healthy(self) -> None:
        if self._disposed:
            raise RuntimeError("StaAutomationThreadManager has been disposed.")
        if self._thread is None or not self._thread.is_alive():
            self._restart_thread()

    def _start_thread(self) -> None:
        with self._thread_lock:
            self._thread = threading.Thread(target=self._process_queue, name="winSpark-STA-Automation", daemon=True)
            self._thread.start()
            self._last_restart_utc = datetime.now(timezone.utc)
            logger.info("STA automation thread started (id=%s)", self._thread.ident)

    def _restart_thread(self) -> None:
        with self._thread_lock:
            self._restart_count += 1
            logger.warning("Restarting STA automation thread (attempt %d)", self._restart_count)
            self._start_thread()

    def _process_queue(self) -> None:
        try:
            import pythoncom

            pythoncom.CoInitialize()
        except ImportError:  # pragma: no cover - exercised only off-Windows
            pythoncom = None

        try:
            while True:
                # UI Automation client calls (e.g. SetFocus, some pattern invocations)
                # can block waiting for message delivery on the calling STA thread —
                # confirmed by reproducing a real hang (target window went "Not
                # Responding") while building this port. A plain blocking
                # queue.get() starves that message pump, so poll instead and pump
                # waiting messages between checks.
                if pythoncom is not None:
                    pythoncom.PumpWaitingMessages()
                try:
                    item = self._queue.get(timeout=0.02)
                except queue.Empty:
                    continue

                if item is None:  # shutdown sentinel
                    break
                if item.future.cancelled():
                    continue
                try:
                    result = item.func()
                    # Update counters before set_result(): set_result() wakes the
                    # awaiting coroutine via call_soon_threadsafe, and it can resume
                    # and read get_health() before this thread runs its next line —
                    # confirmed live as a real race (total_requests_processed read
                    # as 0 right after an awaited call that had already completed).
                    self._total_processed += 1
                    self._last_request_utc = datetime.now(timezone.utc)
                    item.future.set_result(result)
                except Exception as ex:  # noqa: BLE001
                    self._failed_requests += 1
                    self._last_error = str(ex)
                    item.future.set_exception(ex)
                    logger.error("STA automation work item failed", exc_info=True)
        finally:
            if pythoncom is not None:
                pythoncom.CoUninitialize()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "StaAutomationThreadManager":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.dispose()
