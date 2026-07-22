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
async def test_openai_generate_binding_relays_ai_message(tmp_path, monkeypatch):
    """An OpenAI 'generate' binding calls OpenAI (stubbed) and relays the reply
    through the same dedupe/persist/send path as a web source."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    captured = {}

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        captured.update(api_key=api_key, model=model, system_prompt=system_prompt)
        return OpenAiResult.succeeded("AI-written hello")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    factory = ConnectionFactory(tmp_path / "ai.db")
    factory.initialize_schema()
    repository = WhatsAppFetchRelayRepository(factory)
    group_sender = _StubGroupSender()
    mock_server = WhatsAppFetchLocalMockServer()
    scheduler = FetchWebhookBindingScheduler()
    service = WhatsAppFetchRelayService(
        repository, LogRepository(factory), group_sender, mock_server, scheduler,
        openai_config_provider=lambda: ("sk-test", "gpt-4o-mini"),
    )
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Sharon", reply_source="openai", ai_mode="generate", ai_prompt="Be nice."
        )
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert group_sender.calls == [("Sharon", "AI-written hello")]
        assert captured == {"api_key": "sk-test", "model": "gpt-4o-mini", "system_prompt": "Be nice."}
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_ai_generate_uses_provider_base_url(tmp_path, monkeypatch):
    """The relay passes the configured provider's base URL through to the client,
    so pointing at Groq (OpenAI-compatible) works with the same code path."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    seen = {}

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        seen["base_url"] = base_url
        seen["model"] = model
        return OpenAiResult.succeeded("hey")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    group_sender = _StubGroupSender()
    service, repository, mock_server, scheduler = _build_with_ai(
        tmp_path, group_sender,
        config=("gsk-key", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    )
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Sharon", reply_source="openai", ai_mode="generate")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert seen["base_url"] == "https://api.groq.com/openai/v1"
        assert seen["model"] == "llama-3.3-70b-versatile"
        assert group_sender.calls == [("Sharon", "hey")]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_openai_binding_without_key_reports_source_error(tmp_path):
    """No app-wide key configured -> the binding surfaces a plain source error
    rather than silently doing nothing."""
    factory = ConnectionFactory(tmp_path / "nokey.db")
    factory.initialize_schema()
    repository = WhatsAppFetchRelayRepository(factory)
    group_sender = _StubGroupSender()
    mock_server = WhatsAppFetchLocalMockServer()
    scheduler = FetchWebhookBindingScheduler()
    service = WhatsAppFetchRelayService(
        repository, LogRepository(factory), group_sender, mock_server, scheduler,
        openai_config_provider=lambda: ("", "gpt-4o-mini"),
    )
    errors: list[str] = []
    service.on_activity(lambda chat, kind, detail: kind == "source_error" and errors.append(detail))
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Sharon", reply_source="openai", ai_mode="generate")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert group_sender.calls == []
        assert any("AI key" in e for e in errors)
    finally:
        scheduler.dispose()
        mock_server.stop()


class _StubReplyingSender(_StubGroupSender):
    """A stub sender that also exposes the reply-mode reading hook."""

    def __init__(self, incoming_sequence):
        super().__init__()
        self._incoming = list(incoming_sequence)
        self.read_calls = 0

    async def read_last_incoming_message_async(self, group_name):
        self.read_calls += 1
        idx = min(self.read_calls - 1, len(self._incoming) - 1)
        return self._incoming[idx]


def _build_with_ai(tmp_path, group_sender, config=("sk", "gpt-4o-mini")):
    factory = ConnectionFactory(tmp_path / "ai.db")
    factory.initialize_schema()
    repository = WhatsAppFetchRelayRepository(factory)
    mock_server = WhatsAppFetchLocalMockServer()
    scheduler = FetchWebhookBindingScheduler()
    service = WhatsAppFetchRelayService(
        repository, LogRepository(factory), group_sender, mock_server, scheduler,
        openai_config_provider=lambda: config,
    )
    return service, repository, mock_server, scheduler


@pytest.mark.asyncio
async def test_openai_reply_mode_answers_incoming_message(tmp_path, monkeypatch):
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    seen = {}

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        seen["user_message"] = user_message
        return OpenAiResult.succeeded(f"echo: {user_message}")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubReplyingSender(["are you free tomorrow?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Sharon", reply_source="openai", ai_mode="reply", ai_prompt="Be helpful."
        )
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert seen["user_message"] == "are you free tomorrow?"
        assert sender.calls == [("Sharon", "echo: are you free tomorrow?")]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_openai_reply_mode_does_not_reply_twice_to_same_message(tmp_path, monkeypatch):
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        return OpenAiResult.succeeded("sure!")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    # Same incoming message is still newest on the second check.
    sender = _StubReplyingSender(["ping", "ping"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Sharon", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)

        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)

        assert len(sender.calls) == 1  # replied once, deduped on the incoming message
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_openai_reply_mode_stays_quiet_when_no_new_incoming(tmp_path, monkeypatch):
    from winspark.connectors import openai_client

    called = {"generate": 0}

    async def fake_generate(*args, **kwargs):
        called["generate"] += 1
        from winspark.connectors.openai_client import OpenAiResult

        return OpenAiResult.succeeded("hi")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    # Newest message is our own outgoing -> reader returns None -> nothing to do.
    sender = _StubReplyingSender([None])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Sharon", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert sender.calls == []
        assert called["generate"] == 0
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_trigger_mode_replies_when_incoming_matches_literally(tmp_path):
    # No OpenAI key -> literal word matching.
    sender = _StubReplyingSender(["hey are you coming to the party?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender, config=("", ""))
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Sharon", reply_source="trigger",
            trigger_text="coming party", reply_text="Yes, I'll be there!",
        )
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert sender.calls == [("Sharon", "Yes, I'll be there!")]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_trigger_mode_stays_quiet_when_incoming_does_not_match(tmp_path):
    sender = _StubReplyingSender(["what's for dinner?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender, config=("", ""))
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Sharon", reply_source="trigger",
            trigger_text="coming to the party", reply_text="Yes!",
        )
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert sender.calls == []
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_trigger_mode_uses_openai_semantic_match(tmp_path, monkeypatch):
    from winspark.connectors import openai_client

    seen = {}

    async def fake_classify(api_key, model, intent, message, base_url=None):
        seen["intent"] = intent
        seen["message"] = message
        return True  # semantic yes

    monkeypatch.setattr(openai_client, "classify_intent_match_async", fake_classify)

    # Literal match would FAIL here (no shared words) — only semantic passes.
    sender = _StubReplyingSender(["will you show up tonight?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender, config=("sk", "gpt-4o-mini"))
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Sharon", reply_source="trigger",
            trigger_text="asking about my attendance", reply_text="I'll be there!",
        )
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert seen["message"] == "will you show up tonight?"
        assert sender.calls == [("Sharon", "I'll be there!")]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_trigger_mode_replies_once_per_matching_message(tmp_path):
    sender = _StubReplyingSender(["are you coming?", "are you coming?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender, config=("", ""))
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Sharon", reply_source="trigger",
            trigger_text="are you coming", reply_text="Yes!",
        )
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)

        assert len(sender.calls) == 1  # deduped on the incoming message
    finally:
        scheduler.dispose()
        mock_server.stop()


def test_column_migration_adds_missing_binding_columns(tmp_path):
    """A database created before the reply-source columns existed gets them
    added by initialize_schema (idempotent ALTER)."""
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE WhatsAppFetchBindings (
            BindingId TEXT PRIMARY KEY, GroupName TEXT NOT NULL, FetchUrl TEXT NOT NULL,
            ApiKey TEXT NOT NULL DEFAULT '', PollIntervalSeconds INTEGER NOT NULL DEFAULT 3,
            IsEnabled INTEGER NOT NULL DEFAULT 1, LastFetchUtc TEXT, LastFetchState TEXT NOT NULL DEFAULT '',
            LastMessageReceivedUtc TEXT, LastSendUtc TEXT, TotalPolls INTEGER NOT NULL DEFAULT 0,
            TotalSent INTEGER NOT NULL DEFAULT 0, LastError TEXT NOT NULL DEFAULT '',
            CreatedAtUtc TEXT NOT NULL, UpdatedAtUtc TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    factory = ConnectionFactory(db_path)
    factory.initialize_schema()  # should add the missing columns without error
    factory.initialize_schema()  # idempotent: running again is a no-op

    conn = factory.create_connection()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(WhatsAppFetchBindings)").fetchall()}
    conn.close()
    assert {"ReplySource", "AiMode", "AiPrompt"} <= columns


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


def test_strip_web_citations_keeps_words_drops_markup():
    from winspark.connectors.fetch_webhook_relay_service import _strip_web_citations

    raw = ("Final is July 19, 2026, at MetLife Stadium. "
           "([fifa.com](https://fifa.com/x?utm_source=openai)) Kickoff 3 PM."
           + chr(10) + chr(10) + "##")
    assert _strip_web_citations(raw) == "Final is July 19, 2026, at MetLife Stadium. Kickoff 3 PM."
    assert _strip_web_citations("See [the schedule](https://x.com) now.") == "See the schedule now."
    assert _strip_web_citations("plain text stays") == "plain text stays"


@pytest.mark.asyncio
async def test_reply_falls_back_when_the_search_model_fails(monkeypatch, tmp_path):
    """Web lookup on: the search model errors -> the configured model answers,
    so an outage never silences an automation."""
    from winspark.connectors import openai_client
    from winspark.connectors.fetch_webhook_relay_service import WhatsAppFetchRelayService

    factory = ConnectionFactory(tmp_path / "fb.db")
    factory.initialize_schema()
    service = WhatsAppFetchRelayService(
        WhatsAppFetchRelayRepository(factory), LogRepository(factory), _StubGroupSender(),
        WhatsAppFetchLocalMockServer(), FetchWebhookBindingScheduler(),
        openai_config_provider=lambda: ("k", "search-model", "https://api", "normal-model"),
    )

    calls = []

    async def fake_generate(api_key, model, system, user, base_url="", temperature=0.7):
        calls.append(model)
        if model == "search-model":
            return openai_client.OpenAiResult.failed("search model down")
        return openai_client.OpenAiResult.succeeded("fallback answer")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)
    result = await service._generate_with_fallback("k", "search-model", "https://api", "normal-model", "sys", "what is the latest news today?")
    assert result.ok and result.text == "fallback answer"
    assert calls == ["search-model", "normal-model"]


@pytest.mark.asyncio
async def test_smart_routing_only_pays_for_search_when_needed(monkeypatch, tmp_path):
    """Casual messages answer on the cheap configured model; a message that
    needs current info goes straight to the search model."""
    from winspark.connectors import openai_client
    from winspark.connectors.fetch_webhook_relay_service import WhatsAppFetchRelayService

    factory = ConnectionFactory(tmp_path / "sr.db")
    factory.initialize_schema()
    service = WhatsAppFetchRelayService(
        WhatsAppFetchRelayRepository(factory), LogRepository(factory), _StubGroupSender(),
        WhatsAppFetchLocalMockServer(), FetchWebhookBindingScheduler(),
    )
    used = []

    async def fake(api_key, model, system, user, base_url="", temperature=0.7):
        used.append(model)
        return openai_client.OpenAiResult.succeeded("ok")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake)

    used.clear()
    await service._generate_with_fallback("k", "search-model", "u", "normal-model", "s", "good morning")
    assert used == ["normal-model"]              # casual -> cheap model only

    used.clear()
    await service._generate_with_fallback("k", "search-model", "u", "normal-model", "s", "what is the latest news today?")
    assert used == ["search-model"]              # fresh-info -> search model


@pytest.mark.asyncio
async def test_stale_answer_gets_upgraded_to_search(monkeypatch, tmp_path):
    from winspark.connectors import openai_client
    from winspark.connectors.fetch_webhook_relay_service import WhatsAppFetchRelayService

    factory = ConnectionFactory(tmp_path / "up.db")
    factory.initialize_schema()
    service = WhatsAppFetchRelayService(
        WhatsAppFetchRelayRepository(factory), LogRepository(factory), _StubGroupSender(),
        WhatsAppFetchLocalMockServer(), FetchWebhookBindingScheduler(),
    )
    used = []

    async def fake(api_key, model, system, user, base_url="", temperature=0.7):
        used.append(model)
        if model == "normal-model":
            return openai_client.OpenAiResult.succeeded("As of my last update I cannot say.")
        return openai_client.OpenAiResult.succeeded("It is on July 19, 2026.")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake)
    # "who won" isn't in the fresh-info list -> tries cheap first, detects stale, upgrades.
    result = await service._generate_with_fallback("k", "search-model", "u", "normal-model", "s", "who won the toss")
    assert used == ["normal-model", "search-model"]
    assert result.text == "It is on July 19, 2026."


@pytest.mark.asyncio
async def test_ai_reply_remembers_the_chat_across_messages(tmp_path, monkeypatch):
    """The second reply sees the first exchange — per-chat memory, last K only."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    prompts = []

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        prompts.append((system_prompt, user_message))
        return OpenAiResult.succeeded(f"reply #{len(prompts)}")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubReplyingSender(["hi, I'm Dan", "what's my name?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Sharon", reply_source="openai", ai_mode="reply", ai_prompt="Be helpful."
        )
        await service.save_binding_async(binding)

        await service.poll_binding_now_async(binding.binding_id)   # answers "hi, I'm Dan"
        await service.poll_binding_now_async(binding.binding_id)   # answers "what's my name?"

        # First reply: nothing to remember yet — the message goes through plain.
        assert prompts[0][1] == "hi, I'm Dan"
        # Second reply: the prompt carries the remembered exchange, oldest first.
        second_user = prompts[1][1]
        assert "Conversation so far" in second_user
        assert "hi, I'm Dan" in second_user
        assert "You: reply #1" in second_user
        assert second_user.strip().endswith("what's my name?")
        # And the memory itself now holds both exchanges.
        remembered = [(r, t) for r, _, t in repository.get_chat_memory("Sharon")]
        assert remembered == [
            ("them", "hi, I'm Dan"), ("me", "reply #1"),
            ("them", "what's my name?"), ("me", "reply #2"),
        ]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_ai_reply_system_prompt_asks_for_human_texting(tmp_path, monkeypatch):
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    seen = {}

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        seen["system"] = system_prompt
        return OpenAiResult.succeeded("ok")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubReplyingSender(["yo"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Sharon", reply_source="openai", ai_mode="reply", ai_prompt="You are my assistant."
        )
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        # The chat's own prompt stays first (the persona); the texting style rides along.
        assert seen["system"].startswith("You are my assistant.")
        assert "Reply like a person" in seen["system"]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_ai_reply_does_not_regenerate_for_an_already_answered_message(tmp_path, monkeypatch):
    """Every poll used to call the AI again for the same newest message and let
    dedupe discard the result — burning API credit. Now it skips generation."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    calls = {"generate": 0}

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        calls["generate"] += 1
        return OpenAiResult.succeeded("sure!")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubReplyingSender(["ping", "ping", "ping"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Sharon", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)

        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)

        assert len(sender.calls) == 1        # replied exactly once
        assert calls["generate"] == 1        # and only generated once
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_web_posts_land_in_chat_memory_too(stack):
    """What winSpark sends via the inbox link is part of the conversation the
    AI should remember."""
    service, repository, group_sender, mock_server = stack

    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
    await service.save_binding_async(binding)
    mock_server.inject_message("Infosys", "meeting moved to 5pm")
    await service.poll_binding_now_async(binding.binding_id)

    assert repository.get_chat_memory("Infosys") == [("me", "", "meeting moved to 5pm")]


@pytest.mark.asyncio
async def test_deleting_a_binding_keeps_the_chats_memory(tmp_path):
    """Chat memory is PERSISTENT: deleting an automation removes the binding but
    must NOT touch the chat's remembered messages in any store."""
    service, repository, group_sender, mock_server, scheduler = _build(tmp_path)
    try:
        cleared = []

        class _Mem:
            def append_chat_memory(self, *a, **k): pass
            def get_chat_memory(self, *a, **k): return []
            def clear_chat_memory(self, group): cleared.append(group)
            def get_chats_with_memory(self): return []

        service.set_chat_memory(_Mem())

        binding = WhatsAppFetchBindingEntity(group_name="Manohar", reply_source="openai")
        await service.save_binding_async(binding)
        await service.delete_binding_async(binding.binding_id)

        assert cleared == []                        # memory was left untouched
        assert service.get_bindings() == []         # only the binding was removed
    finally:
        scheduler.dispose()
        mock_server.stop()
