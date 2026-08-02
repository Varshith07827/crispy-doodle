"""Port of WinSpark.Infrastructure.Services.WhatsApp.FetchWebhookBindingScheduler.

Independent per-binding poll loops with concurrent-tick protection. The .NET
version uses System.Threading.Timer per binding; this uses one asyncio task
per binding running its own sleep loop — the more idiomatic Python
equivalent, same behavior (staggered start, skip-if-already-running tick,
clean stop on unsync).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from winspark.connectors.fetch_webhook_models import FetchWebhookDefaults, WhatsAppFetchBindingEntity

logger = logging.getLogger(__name__)

_STAGGER_SECONDS_PER_BINDING = 3.0

BindingPollHandler = Callable[[str], Awaitable[None]]


class FetchWebhookBindingScheduler:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._poll_locks: dict[str, asyncio.Lock] = {}
        self._relay_enabled = False
        self._polling_suspended = False
        self._on_binding_poll_requested: Optional[BindingPollHandler] = None

    def set_binding_poll_requested_handler(self, handler: Optional[BindingPollHandler]) -> None:
        self._on_binding_poll_requested = handler

    def set_relay_enabled(self, enabled: bool) -> None:
        self._relay_enabled = enabled

    def set_polling_suspended(self, suspended: bool) -> None:
        self._polling_suspended = suspended

    @property
    def is_polling_suspended(self) -> bool:
        return self._polling_suspended

    def sync_bindings(self, bindings: list[WhatsAppFetchBindingEntity]) -> None:
        active: set[str] = set()
        if self._relay_enabled:
            enabled = sorted((b for b in bindings if b.is_enabled), key=lambda b: b.group_name.lower())
            for index, binding in enumerate(enabled):
                active.add(binding.binding_id)
                self.start_or_update(
                    binding.binding_id, binding.poll_interval_seconds, index * _STAGGER_SECONDS_PER_BINDING
                )

        for binding_id in list(self._tasks):
            if binding_id not in active:
                self.stop_binding(binding_id)

    def start_or_update(self, binding_id: str, poll_interval_seconds: int, initial_delay_seconds: float = 0) -> None:
        interval = max(FetchWebhookDefaults.MIN_POLL_INTERVAL_SECONDS, poll_interval_seconds)
        self.stop_binding(binding_id)
        self._tasks[binding_id] = asyncio.create_task(self._run_loop(binding_id, interval, max(0.0, initial_delay_seconds)))

    def stop_binding(self, binding_id: str) -> None:
        task = self._tasks.pop(binding_id, None)
        if task is not None:
            task.cancel()

    async def poll_now_async(self, binding_id: str) -> None:
        await self._request_poll(binding_id)

    async def _run_loop(self, binding_id: str, interval_seconds: float, initial_delay_seconds: float) -> None:
        try:
            await asyncio.sleep(initial_delay_seconds if initial_delay_seconds > 0 else interval_seconds)
            while True:
                if not self._polling_suspended:
                    await self._request_poll(binding_id)
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            pass

    async def _request_poll(self, binding_id: str) -> None:
        lock = self._poll_locks.setdefault(binding_id, asyncio.Lock())
        if lock.locked():
            return  # a poll for this binding is already in flight — skip this tick

        async with lock:
            if self._on_binding_poll_requested is not None:
                try:
                    await self._on_binding_poll_requested(binding_id)
                except Exception:  # noqa: BLE001
                    logger.exception("Binding poll handler failed for %s", binding_id)

    def dispose(self) -> None:
        for binding_id in list(self._tasks):
            self.stop_binding(binding_id)
