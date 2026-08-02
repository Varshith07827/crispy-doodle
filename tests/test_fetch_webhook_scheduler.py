"""Tests FetchWebhookBindingScheduler: staggered start, concurrent-tick
protection, poll-now bypass, suspend/resume, and clean stop. Uses a tiny
monkeypatched MIN_POLL_INTERVAL_SECONDS so real-time asyncio.sleep-based
tests stay fast rather than waiting the real 3-second production floor.
"""

import asyncio

import pytest

from winspark.connectors.fetch_webhook_models import FetchWebhookDefaults, WhatsAppFetchBindingEntity
from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler


@pytest.fixture(autouse=True)
def _fast_min_interval(monkeypatch):
    monkeypatch.setattr(FetchWebhookDefaults, "MIN_POLL_INTERVAL_SECONDS", 0.05)


@pytest.mark.asyncio
async def test_poll_now_triggers_handler_immediately():
    scheduler = FetchWebhookBindingScheduler()
    calls = []

    async def handler(binding_id):
        calls.append(binding_id)

    scheduler.set_binding_poll_requested_handler(handler)
    await scheduler.poll_now_async("binding-1")

    assert calls == ["binding-1"]


@pytest.mark.asyncio
async def test_concurrent_ticks_for_same_binding_are_skipped():
    scheduler = FetchWebhookBindingScheduler()
    started = asyncio.Event()
    calls = []

    async def slow_handler(binding_id):
        calls.append(binding_id)
        started.set()
        await asyncio.sleep(0.2)

    scheduler.set_binding_poll_requested_handler(slow_handler)

    first = asyncio.create_task(scheduler.poll_now_async("binding-1"))
    await started.wait()
    # a second request while the first is still running should be skipped, not queued
    await scheduler.poll_now_async("binding-1")
    await first

    assert calls == ["binding-1"]


@pytest.mark.asyncio
async def test_sync_bindings_only_starts_enabled_bindings_when_relay_enabled():
    scheduler = FetchWebhookBindingScheduler()
    ticks = []

    async def handler(binding_id):
        ticks.append(binding_id)

    scheduler.set_binding_poll_requested_handler(handler)
    scheduler.set_relay_enabled(True)

    bindings = [
        WhatsAppFetchBindingEntity(binding_id="a", group_name="A", is_enabled=True, poll_interval_seconds=0),
        WhatsAppFetchBindingEntity(binding_id="b", group_name="B", is_enabled=False, poll_interval_seconds=0),
    ]
    scheduler.sync_bindings(bindings)

    try:
        await asyncio.sleep(0.3)
        assert "a" in ticks
        assert "b" not in ticks
    finally:
        scheduler.dispose()


@pytest.mark.asyncio
async def test_sync_bindings_with_relay_disabled_starts_nothing():
    scheduler = FetchWebhookBindingScheduler()
    ticks = []

    async def handler(binding_id):
        ticks.append(binding_id)

    scheduler.set_binding_poll_requested_handler(handler)
    scheduler.set_relay_enabled(False)
    scheduler.sync_bindings([WhatsAppFetchBindingEntity(binding_id="a", group_name="A", is_enabled=True, poll_interval_seconds=0)])

    try:
        await asyncio.sleep(0.2)
        assert ticks == []
    finally:
        scheduler.dispose()


@pytest.mark.asyncio
async def test_stop_binding_cancels_further_ticks():
    scheduler = FetchWebhookBindingScheduler()
    ticks = []

    async def handler(binding_id):
        ticks.append(binding_id)

    scheduler.set_binding_poll_requested_handler(handler)
    scheduler.set_relay_enabled(True)
    scheduler.sync_bindings([WhatsAppFetchBindingEntity(binding_id="a", group_name="A", is_enabled=True, poll_interval_seconds=0)])

    await asyncio.sleep(0.15)
    scheduler.stop_binding("a")
    count_after_stop = len(ticks)
    await asyncio.sleep(0.15)

    assert len(ticks) == count_after_stop


@pytest.mark.asyncio
async def test_suspended_polling_pauses_ticks_without_stopping_the_task():
    scheduler = FetchWebhookBindingScheduler()
    ticks = []

    async def handler(binding_id):
        ticks.append(binding_id)

    scheduler.set_binding_poll_requested_handler(handler)
    scheduler.set_relay_enabled(True)
    scheduler.sync_bindings([WhatsAppFetchBindingEntity(binding_id="a", group_name="A", is_enabled=True, poll_interval_seconds=0)])

    try:
        scheduler.set_polling_suspended(True)
        assert scheduler.is_polling_suspended is True
        await asyncio.sleep(0.2)
        assert ticks == []

        scheduler.set_polling_suspended(False)
        await asyncio.sleep(0.2)
        assert len(ticks) > 0
    finally:
        scheduler.dispose()
