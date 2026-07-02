"""End-to-end test of the Fetch-Webhook orchestrator: a real
WhatsAppFetchRelayService wired to a real local mock HTTP server (real
socket round-trips), a real SQLite database, and a real scheduler — with a
STUB group sender instead of a real one, since exercising the real sender
would actually deliver a message to a real WhatsApp contact (see
PORT_NOTES.md and test_whatsapp_group_sender.py for why that's tested
separately and more carefully). Runs on any platform.
"""

import asyncio
import socket

import pytest

from winspark.connectors.fetch_webhook_models import (
    FetchWebhookDefaults,
    WhatsAppFetchBindingEntity,
    WhatsAppFetchRelayMessageState,
    WhatsAppGroupSendResult,
)
from winspark.connectors.fetch_webhook_mock_server import WhatsAppFetchLocalMockServer
from winspark.connectors.fetch_webhook_relay_service import WhatsAppFetchRelayService
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository
from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler
from winspark.data.connection import ConnectionFactory
from winspark.data.repositories import LogRepository


class _StubGroupSender:
    def __init__(self, fail_times: int = 0):
        self.calls: list[tuple[str, str]] = []
        self._fail_times = fail_times

    async def send_to_group_async(self, group_name, message_text):
        self.calls.append((group_name, message_text))
        if len(self.calls) <= self._fail_times:
            return WhatsAppGroupSendResult.failed("stub failure")
        return WhatsAppGroupSendResult.succeeded("sent", verified=True, appeared=True)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch):
    monkeypatch.setattr(FetchWebhookDefaults, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(FetchWebhookDefaults, "MIN_POLL_INTERVAL_SECONDS", 0.05)
    # Use a random free port for the mock server (mock_url_for_group + ensure_started
    # both read MOCK_PORT at runtime) so these tests don't collide with a running
    # app/GUI that already holds the default 5001.
    monkeypatch.setattr(FetchWebhookDefaults, "MOCK_PORT", _free_port())


def _build(tmp_path, fail_times: int = 0):
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()

    repository = WhatsAppFetchRelayRepository(factory)
    log_repository = LogRepository(factory)
    group_sender = _StubGroupSender(fail_times=fail_times)
    mock_server = WhatsAppFetchLocalMockServer()
    scheduler = FetchWebhookBindingScheduler()

    service = WhatsAppFetchRelayService(repository, log_repository, group_sender, mock_server, scheduler)
    return service, repository, group_sender, mock_server, scheduler


@pytest.fixture
def stack(tmp_path):
    service, repository, group_sender, mock_server, scheduler = _build(tmp_path)
    try:
        yield service, repository, group_sender, mock_server
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_poll_binding_relays_injected_message_to_group_sender(stack):
    service, repository, group_sender, mock_server = stack

    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
    await service.save_binding_async(binding)  # empty fetch_url -> normalized to the local mock's poll URL

    mock_server.inject_message("Infosys", "hello from the AI")

    await service.poll_binding_now_async(binding.binding_id)

    assert group_sender.calls == [("Infosys", "hello from the AI")]
    messages = repository.get_recent_messages()
    assert len(messages) == 1
    assert messages[0].state == WhatsAppFetchRelayMessageState.SENT


@pytest.mark.asyncio
async def test_activity_events_are_emitted_across_a_successful_relay(stack):
    service, repository, group_sender, mock_server = stack
    events: list[tuple[str, str]] = []
    service.on_activity(lambda chat, kind, detail: events.append((chat, kind)))

    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
    await service.save_binding_async(binding)
    mock_server.inject_message("Infosys", "hi")
    await service.poll_binding_now_async(binding.binding_id)

    kinds = [k for _, k in events]
    assert "checking" in kinds
    assert "received" in kinds
    assert "sending" in kinds
    assert "sent" in kinds
    assert all(chat == "Infosys" for chat, _ in events)


@pytest.mark.asyncio
async def test_activity_reports_source_error_on_bad_url(stack):
    service, repository, group_sender, mock_server = stack
    events: list[tuple[str, str, str]] = []
    service.on_activity(lambda chat, kind, detail: events.append((chat, kind, detail)))

    # A non-localhost URL that won't resolve -> fetch error -> source_error activity.
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://127.0.0.1:1/nope")
    await service.save_binding_async(binding)
    await service.poll_binding_now_async(binding.binding_id)

    assert any(kind == "source_error" for _, kind, _ in events)


@pytest.mark.asyncio
async def test_poll_with_empty_queue_does_not_call_group_sender(stack):
    service, repository, group_sender, mock_server = stack
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
    await service.save_binding_async(binding)

    await service.poll_binding_now_async(binding.binding_id)

    assert group_sender.calls == []
    assert repository.get_binding(binding.binding_id).last_fetch_state == "blank"


@pytest.mark.asyncio
async def test_external_id_dedup_skips_already_sent_message(stack):
    service, repository, group_sender, mock_server = stack
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
    await service.save_binding_async(binding)

    mock_server.inject_message("Infosys", '{"id": "msg-1", "message": "first send"}')
    await service.poll_binding_now_async(binding.binding_id)
    assert len(group_sender.calls) == 1

    # Re-inject the exact same external id — should be recognized as already sent.
    mock_server.inject_message("Infosys", '{"id": "msg-1", "message": "first send"}')
    await service.poll_binding_now_async(binding.binding_id)

    assert len(group_sender.calls) == 1  # not called again
    assert repository.get_binding(binding.binding_id).last_fetch_state == "duplicate"


@pytest.mark.asyncio
async def test_retries_until_success_within_max_attempts(tmp_path):
    service, repository, group_sender, mock_server, scheduler = _build(tmp_path, fail_times=1)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
        await service.save_binding_async(binding)
        mock_server.inject_message("Infosys", "retry me")

        await service.poll_binding_now_async(binding.binding_id)  # first attempt fails
        assert len(group_sender.calls) == 1
        messages = repository.get_recent_messages()
        assert messages[0].state == WhatsAppFetchRelayMessageState.RETRYING

        await asyncio.sleep(0.05)
        await service.set_relay_enabled_async(True)
        await service.process_tick_async()  # retry sweep picks it up and succeeds

        assert len(group_sender.calls) == 2
        assert repository.get_recent_messages()[0].state == WhatsAppFetchRelayMessageState.SENT
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_message_marked_failed_after_max_send_attempts(tmp_path):
    service, repository, group_sender, mock_server, scheduler = _build(tmp_path, fail_times=99)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
        await service.save_binding_async(binding)
        mock_server.inject_message("Infosys", "always fails")
        await service.set_relay_enabled_async(True)

        await service.poll_binding_now_async(binding.binding_id)
        await asyncio.sleep(0.05)
        await service.process_tick_async()
        await asyncio.sleep(0.05)
        await service.process_tick_async()

        message = repository.get_recent_messages()[0]
        assert message.attempt_count == FetchWebhookDefaults.MAX_SEND_ATTEMPTS
        assert message.state == WhatsAppFetchRelayMessageState.FAILED
        assert repository.get_binding(binding.binding_id).last_fetch_state == "send-failed"
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_full_enable_flow_polls_automatically_via_scheduler(tmp_path):
    service, repository, group_sender, mock_server, scheduler = _build(tmp_path)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="", poll_interval_seconds=0, is_enabled=True)
        await service.save_binding_async(binding)
        mock_server.inject_message("Infosys", "auto polled message")

        await service.set_relay_enabled_async(True)
        await asyncio.sleep(0.3)

        assert ("Infosys", "auto polled message") in group_sender.calls
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_disabled_binding_is_not_polled(stack):
    service, repository, group_sender, mock_server = stack
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="", is_enabled=False)
    await service.save_binding_async(binding)
    mock_server.inject_message("Infosys", "should not be sent")

    await service.poll_binding_now_async(binding.binding_id)

    assert group_sender.calls == []


@pytest.mark.asyncio
async def test_pause_then_resume_binding(stack):
    service, repository, group_sender, mock_server = stack
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
    await service.save_binding_async(binding)

    await service.pause_binding_async(binding.binding_id)
    assert repository.get_binding(binding.binding_id).is_enabled is False

    mock_server.inject_message("Infosys", "queued while paused")
    await service.resume_binding_async(binding.binding_id)

    assert repository.get_binding(binding.binding_id).is_enabled is True
