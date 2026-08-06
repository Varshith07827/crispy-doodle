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
        assert captured["api_key"] == "sk-test" and captured["model"] == "gpt-4o-mini"
        # The persona is kept, with the current date/time appended for reference.
        assert captured["system_prompt"].startswith("Be nice.")
        assert "current local date and time" in captured["system_prompt"]
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
        assert "Recent conversation" in second_user
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


class _RecentMsg:
    def __init__(self, text, sender="", time_text=""):
        self.text, self.sender, self.is_incoming = text, sender, True
        self.time_text = time_text


class _StubRecentSender(_StubGroupSender):
    """Exposes the richer recent-incoming read; the test drives `incoming`."""

    def __init__(self):
        super().__init__()
        self.incoming = []  # oldest-first list of _RecentMsg

    async def read_recent_incoming_async(self, group_name, limit=10):
        return list(self.incoming)


@pytest.mark.asyncio
async def test_reply_mode_does_not_skip_messages_arriving_between_polls(tmp_path, monkeypatch):
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    answered = []

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        # The target is the line after "answer this one:" (or the whole thing).
        target = user_message.split("answer this one:\n")[-1]
        answered.append(target)
        return OpenAiResult.succeeded("ok")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Papa", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)

        # Baseline: one message present — answered as the newest.
        sender.incoming = [_RecentMsg("hey")]
        await service.poll_binding_now_async(binding.binding_id)

        # Now TWO messages arrive before the next poll — the exact bug case.
        sender.incoming = [_RecentMsg("hey"), _RecentMsg("Where is London"), _RecentMsg("Where is punjab")]
        await service.poll_binding_now_async(binding.binding_id)   # -> oldest unanswered
        await service.poll_binding_now_async(binding.binding_id)   # -> the next one
        await service.poll_binding_now_async(binding.binding_id)   # -> nothing new

        # Both were answered, in order — London was NOT skipped.
        assert answered == ["hey", "Where is London", "Where is punjab"]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_reply_mode_answers_a_repeated_message_again(tmp_path, monkeypatch):
    """Same defect as the command-mode one: identity was hash(text), so saying
    the same thing twice meant the second one silently got no reply — for ever,
    not just within one poll."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    answered = []

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        answered.append(user_message.split("answer this one:\n")[-1])
        return OpenAiResult.succeeded("ok")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Papa", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)

        sender.incoming = [_RecentMsg("are you free?", time_text="6:00 pm")]
        await service.poll_binding_now_async(binding.binding_id)

        # Asked again later — must be answered again, not written off as a dupe.
        sender.incoming += [_RecentMsg("are you free?", time_text="6:30 pm")]
        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)   # nothing left

        assert answered == ["are you free?", "are you free?"]
        assert len(sender.calls) == 2
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_the_relay_does_not_re_store_what_memory_already_has(tmp_path, monkeypatch):
    """Chat memory has TWO independent writers — this relay and the live-view
    reader that records the same conversation off the screen. Neither used to
    check the other, so a real store ended up with the same question filed
    three times."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        return OpenAiResult.succeeded("Rabindranath Tagore.")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Varshith", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)

        question = "who wrote the national anthem"
        # The live-view reader got there first — both the question and, after the
        # send, our answer are already in memory.
        service._memory.append_chat_memory("Varshith", "them", "V", question, keep=400)

        sender.incoming = [_RecentMsg(question, "V", time_text="2:00 pm")]
        await service.poll_binding_now_async(binding.binding_id)

        stored = [t for _r, _s, t in service._memory.get_chat_memory("Varshith", 50)]
        assert stored.count(question) == 1                      # not stored twice
        assert stored.count("Rabindranath Tagore.") == 1        # the reply, once
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_a_genuinely_new_message_is_still_stored(tmp_path, monkeypatch):
    """The guard must not become 'never store anything'."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        return OpenAiResult.succeeded("ok")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Varshith", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)

        sender.incoming = [_RecentMsg("something nobody stored yet", "V", time_text="2:00 pm")]
        await service.poll_binding_now_async(binding.binding_id)

        stored = [t for _r, _s, t in service._memory.get_chat_memory("Varshith", 50)]
        assert "something nobody stored yet" in stored
        assert "ok" in stored
    finally:
        scheduler.dispose()
        mock_server.stop()


# --- a send that will never work must stop being retried ---------------------

@pytest.mark.asyncio
async def test_a_permanently_failed_reply_is_not_regenerated_for_ever(tmp_path, monkeypatch):
    """The loop that made a single broken send unbearable: a reply row goes
    FAILED after MAX_SEND_ATTEMPTS, but "already answered" only recognised
    SENT — so every poll three seconds later re-picked the same message, paid
    for a fresh AI reply, and re-entered the send path."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    ai_calls = []

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        ai_calls.append(user_message)
        return OpenAiResult.succeeded("an answer")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubRecentSender()
    sender._fail_times = 99          # every send fails, for ever
    sender.incoming = [_RecentMsg("!bot hello", "V", time_text="1:00 pm")]

    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Varshith", reply_source="openai", ai_mode="command", trigger_text="bot")
        await service.save_binding_async(binding)

        for _ in range(6):
            await service.poll_binding_now_async(binding.binding_id)
        settled_ai, settled_sends = len(ai_calls), len(sender.calls)

        for _ in range(10):          # keep polling long after it gave up
            await service.poll_binding_now_async(binding.binding_id)

        # The point: it STOPS. Sending is capped by the attempt limit, and once
        # it has given up neither another AI call nor another paste happens —
        # where before, every tick bought a new reply and re-entered the send.
        assert len(sender.calls) <= FetchWebhookDefaults.MAX_SEND_ATTEMPTS
        assert len(ai_calls) == settled_ai
        assert len(sender.calls) == settled_sends
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_a_later_message_is_still_answered_after_one_gave_up(tmp_path, monkeypatch):
    """Giving up on one reply must not make the bot deaf to the next."""
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    answered = []

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        answered.append(user_message.split("answer this:\n")[-1])
        return OpenAiResult.succeeded("ok")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubRecentSender()
    sender._fail_times = FetchWebhookDefaults.MAX_SEND_ATTEMPTS   # first one never lands
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Varshith", reply_source="openai", ai_mode="command", trigger_text="bot")
        await service.save_binding_async(binding)

        sender.incoming = [_RecentMsg("!bot first", "V", time_text="1:00 pm")]
        for _ in range(4):
            await service.poll_binding_now_async(binding.binding_id)

        sender.incoming.append(_RecentMsg("!bot second", "V", time_text="1:05 pm"))
        await service.poll_binding_now_async(binding.binding_id)

        assert "second" in answered[-1]
    finally:
        scheduler.dispose()
        mock_server.stop()


# --- command mode: answer only when addressed by name ("!winspark …") --------

def _command_capture(monkeypatch, answered, systems=None):
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        answered.append(user_message.split("answer this:\n")[-1])
        if systems is not None:
            systems.append(system_prompt)
        return OpenAiResult.succeeded("ok")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)


@pytest.mark.asyncio
async def test_command_mode_ignores_chatter_and_answers_only_when_called(tmp_path, monkeypatch):
    answered: list[str] = []
    _command_capture(monkeypatch, answered)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Papa", reply_source="openai", ai_mode="command", trigger_text="winspark")
        await service.save_binding_async(binding)

        # Ordinary conversation — the bot must stay completely silent.
        sender.incoming = [_RecentMsg("hey"), _RecentMsg("I was reading about winspark")]
        await service.poll_binding_now_async(binding.binding_id)
        assert answered == []
        assert sender.calls == []

        # Now it's actually called.
        sender.incoming = sender.incoming + [_RecentMsg("!winspark what's the weather")]
        await service.poll_binding_now_async(binding.binding_id)

        # Answered the QUESTION, with the addressing stripped off.
        assert answered == ["what's the weather"]
        assert len(sender.calls) == 1
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_command_mode_answers_backlogged_calls_without_deadlocking(tmp_path, monkeypatch):
    """Two people call the bot inside one poll interval, with ordinary chatter
    in between. Naively reusing the plain reply picker deadlocks here: it hands
    back the oldest message with no SENT row, which is chatter it must never
    answer, so it returns that same message forever and the real calls behind it
    are never reached."""
    answered: list[str] = []
    _command_capture(monkeypatch, answered)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Papa", reply_source="openai", ai_mode="command", trigger_text="winspark")
        await service.save_binding_async(binding)

        sender.incoming = [_RecentMsg("!winspark first question")]
        await service.poll_binding_now_async(binding.binding_id)

        sender.incoming = [
            _RecentMsg("!winspark first question"),
            _RecentMsg("unrelated chatter"),               # must never be answered
            _RecentMsg("!winspark second question", "Ravi"),
            _RecentMsg("more chatter"),                    # nor this
            _RecentMsg("!winspark third question", "Asha"),
        ]
        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)

        # Both later calls answered, in order; neither chatter line ever was.
        assert answered == [
            "first question",
            "Ravi: second question",
            "Asha: third question",
        ]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_command_mode_does_not_backfill_old_calls_when_switched_on(tmp_path, monkeypatch):
    """Turning this on in a chat that already contains old "!winspark" messages
    must not fire a burst of replies to all of them."""
    answered: list[str] = []
    _command_capture(monkeypatch, answered)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Papa", reply_source="openai", ai_mode="command", trigger_text="winspark")
        await service.save_binding_async(binding)

        sender.incoming = [
            _RecentMsg("!winspark old one"),
            _RecentMsg("!winspark old two"),
            _RecentMsg("!winspark newest"),
        ]
        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)

        assert answered == ["newest"]   # only the newest; history left alone
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_command_mode_answers_a_repeated_question_again(tmp_path, monkeypatch):
    """Reported live: after two questions were answered, sending the SAME two
    again got no reply at all, permanently.

    A message's identity was hash(text), so an identical message collided with
    the already-SENT row for the first one and was skipped as a duplicate. Every
    command here is a repeat of an earlier one — the exact transcript that
    failed."""
    answered: list[str] = []
    _command_capture(monkeypatch, answered)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Varshith", reply_source="openai", ai_mode="command", trigger_text="nagenai")
        await service.save_binding_async(binding)

        weather = "!nagenai how's the weather in Goa on 3rd August"
        model = "!nagenai can you tell me what model you are ?"

        sender.incoming = [_RecentMsg(weather, time_text="1:00 pm")]
        await service.poll_binding_now_async(binding.binding_id)
        sender.incoming += [_RecentMsg(model, time_text="1:01 pm")]
        await service.poll_binding_now_async(binding.binding_id)
        assert answered == ["how's the weather in Goa on 3rd August",
                            "can you tell me what model you are ?"]

        # Both asked again a minute later — these were the ones silently dropped.
        sender.incoming += [
            _RecentMsg(model, time_text="1:02 pm"),
            _RecentMsg(weather, time_text="1:02 pm"),
        ]
        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)

        assert answered == [
            "how's the weather in Goa on 3rd August",
            "can you tell me what model you are ?",
            "can you tell me what model you are ?",
            "how's the weather in Goa on 3rd August",
        ]
        assert len(sender.calls) == 4
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_command_mode_answers_the_same_text_twice_in_one_minute(tmp_path, monkeypatch):
    """Identical text AND identical timestamp — the timestamp alone can't tell
    these apart, so identity also counts occurrences."""
    answered: list[str] = []
    _command_capture(monkeypatch, answered)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Papa", reply_source="openai", ai_mode="command", trigger_text="winspark")
        await service.save_binding_async(binding)

        sender.incoming = [_RecentMsg("!winspark hi", time_text="1:02 pm")]
        await service.poll_binding_now_async(binding.binding_id)
        sender.incoming += [_RecentMsg("!winspark hi", time_text="1:02 pm")]
        await service.poll_binding_now_async(binding.binding_id)
        await service.poll_binding_now_async(binding.binding_id)   # nothing left

        assert answered == ["hi", "hi"]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_command_mode_does_not_re_answer_pre_upgrade_messages(tmp_path, monkeypatch):
    """Rows saved before identities carried a timestamp used the bare text hash.
    They must still count as answered, or upgrading would fire a burst of
    replies at commands already dealt with."""
    from winspark.connectors.fetch_webhook_models import (
        WhatsAppFetchRelayMessageEntity,
        WhatsAppFetchRelayMessageState,
    )
    from winspark.connectors.fetch_webhook_repository import compute_content_hash

    answered: list[str] = []
    _command_capture(monkeypatch, answered)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Papa", reply_source="openai", ai_mode="command", trigger_text="winspark")
        await service.save_binding_async(binding)

        # A legacy SENT row: keyed on the text alone, no timestamp.
        old = "!winspark old question"
        repository.insert_message(WhatsAppFetchRelayMessageEntity(
            binding_id=binding.binding_id,
            external_id="reply:" + compute_content_hash(old),
            message_text="an answer from before the upgrade",
            content_hash=compute_content_hash("an answer from before the upgrade"),
            state=WhatsAppFetchRelayMessageState.SENT,
        ))

        sender.incoming = [_RecentMsg(old, time_text="12:00 pm")]
        await service.poll_binding_now_async(binding.binding_id)
        assert answered == []          # already handled before the upgrade

        # A genuinely new call still gets through.
        sender.incoming += [_RecentMsg("!winspark new question", time_text="12:05 pm")]
        await service.poll_binding_now_async(binding.binding_id)
        assert answered == ["new question"]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_command_mode_answers_each_call_exactly_once(tmp_path, monkeypatch):
    answered: list[str] = []
    _command_capture(monkeypatch, answered)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Papa", reply_source="openai", ai_mode="command", trigger_text="winspark")
        await service.save_binding_async(binding)

        sender.incoming = [_RecentMsg("!winspark hello")]
        for _ in range(4):
            await service.poll_binding_now_async(binding.binding_id)

        assert answered == ["hello"]
        assert len(sender.calls) == 1
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_command_mode_tells_the_ai_it_is_the_one_being_addressed(tmp_path, monkeypatch):
    """The plain reply mode's prompt makes the AI answer AS the account owner.
    A bot called by name is the one being asked, so the framing has to change."""
    answered: list[str] = []
    systems: list[str] = []
    _command_capture(monkeypatch, answered, systems)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Papa", reply_source="openai", ai_mode="command",
            trigger_text="winspark", ai_prompt="Be concise.")
        await service.save_binding_async(binding)
        sender.incoming = [_RecentMsg("!winspark hi")]
        await service.poll_binding_now_async(binding.binding_id)

        assert '"winspark"' in systems[0]
        assert "!winspark" in systems[0]
        assert "Be concise." in systems[0]      # the user's own persona survives
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_command_mode_without_a_command_word_reports_instead_of_replying(tmp_path, monkeypatch):
    """A misconfigured binding must stay silent, not answer everything."""
    answered: list[str] = []
    _command_capture(monkeypatch, answered)

    sender = _StubRecentSender()
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    errors: list[str] = []
    service.on_activity(lambda chat, kind, detail: kind == "source_error" and errors.append(detail))
    try:
        binding = WhatsAppFetchBindingEntity(
            group_name="Papa", reply_source="openai", ai_mode="command", trigger_text="")
        await service.save_binding_async(binding)
        sender.incoming = [_RecentMsg("!winspark hello"), _RecentMsg("anything")]
        await service.poll_binding_now_async(binding.binding_id)

        assert answered == []
        assert sender.calls == []
        assert any("command word" in e for e in errors)
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_reply_injects_current_date_and_time(tmp_path, monkeypatch):
    from datetime import datetime

    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    captured = {}

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        captured["system"] = system_prompt
        return OpenAiResult.succeeded("ok")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    sender = _StubReplyingSender(["what time is it?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Papa", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        # The model is now told the real local date and time, so it can answer.
        assert "current local date and time" in captured["system"]
        assert datetime.now().strftime("%A") in captured["system"]  # today's weekday
    finally:
        scheduler.dispose()
        mock_server.stop()


def _seed_archive(repository, group, messages):
    """Append (role, text) pairs straight into a chat's memory store."""
    for role, text in messages:
        repository.append_chat_memory(group, role, "", text, keep=1000)


@pytest.mark.asyncio
async def test_rag_retrieves_a_relevant_older_message_lexical(tmp_path, monkeypatch):
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    prompt = {}

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        prompt["user"] = user_message
        return OpenAiResult.succeeded("ok")

    async def no_embeddings(*a, **k):
        return None  # force the lexical path

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)
    monkeypatch.setattr(openai_client, "embed_texts_async", no_embeddings)

    sender = _StubReplyingSender(["how much is the exam fee?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        # Old, relevant fact, then lots of filler so it falls OUT of the recent
        # window and can only reach the prompt via retrieval.
        _seed_archive(repository, "Papa", [("them", "the exam fee is 45000, due January 15")])
        _seed_archive(repository, "Papa", [("me", f"filler chit chat number {i}") for i in range(20)])

        binding = WhatsAppFetchBindingEntity(group_name="Papa", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        user = prompt["user"]
        assert "Relevant earlier messages" in user
        assert "exam fee is 45000" in user            # the old fact was retrieved
        assert "filler chit chat number 0" not in user  # irrelevant filler was not
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_rag_uses_embeddings_when_available(tmp_path, monkeypatch):
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    prompt = {}

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        prompt["user"] = user_message
        return OpenAiResult.succeeded("ok")

    async def fake_embed(api_key, texts, base_url=None, model=None):
        # "fee" messages point one way, everything else the other — so cosine
        # cleanly separates the relevant fact from filler.
        return [[1.0, 0.0] if "fee" in t.lower() else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)
    monkeypatch.setattr(openai_client, "embed_texts_async", fake_embed)

    sender = _StubReplyingSender(["what about the fee?"])
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        _seed_archive(repository, "Papa", [("them", "the fee is 45000")])
        _seed_archive(repository, "Papa", [("me", f"unrelated line {i}") for i in range(20)])

        binding = WhatsAppFetchBindingEntity(group_name="Papa", reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        assert "Relevant earlier messages" in prompt["user"]
        assert "the fee is 45000" in prompt["user"]
    finally:
        scheduler.dispose()
        mock_server.stop()


class _Row:
    def __init__(self, chat_name):
        self.chat_name = chat_name
        self.raw_text = chat_name


class _CanonSender(_StubReplyingSender):
    """A replying sender that also RESOLVES a bound number/name to a canonical
    chat name — so memory keys can be unified."""

    def __init__(self, incoming_sequence, canonical):
        super().__init__(incoming_sequence)
        self._canonical = canonical

    async def resolve_chat_row_async(self, group_name):
        return 4242, _Row(self._canonical)


@pytest.mark.asyncio
async def test_memory_is_keyed_by_canonical_chat_name(tmp_path, monkeypatch):
    from winspark.connectors import openai_client
    from winspark.connectors.openai_client import OpenAiResult

    async def fake_generate(api_key, model, system_prompt, user_message, base_url=None):
        return OpenAiResult.succeeded("noted")

    monkeypatch.setattr(openai_client, "generate_reply_async", fake_generate)

    # Bound by phone number, but the chat resolves to the contact name "Papa".
    sender = _CanonSender(["hey"], canonical="Papa")
    service, repository, mock_server, scheduler = _build_with_ai(tmp_path, sender)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="+91 79811 49423",
                                             reply_source="openai", ai_mode="reply")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        # Memory lands under the canonical "Papa", NOT the typed number — so it
        # unifies with what the live-view path stores.
        assert [t for _r, _s, t in repository.get_chat_memory("Papa")] == ["hey", "noted"]
        assert repository.get_chat_memory("+91 79811 49423") == []
    finally:
        scheduler.dispose()
        mock_server.stop()


# --- the durable failure record -----------------------------------------
#
# Regression tests for a session that visibly failed and left NO trace: the
# Logs table held 19 rows, every one "Information — sent". A failure reached
# only the in-memory Activity list, `logger.warning` (stderr, which the
# packaged windowed .exe does not have), and the binding's LastError column —
# which the next poll 3 seconds later overwrote. Nothing survived to diagnose.


def _failing_fetch(monkeypatch, reason_box: dict):
    """Make every web poll fail with whatever reason_box['reason'] currently is."""
    from winspark.connectors import fetch_webhook_relay_service
    from winspark.connectors.fetch_webhook_models import WhatsAppFetchApiResult

    async def fake_fetch(fetch_url, api_key):
        return WhatsAppFetchApiResult.failed(reason_box["reason"])

    monkeypatch.setattr(
        fetch_webhook_relay_service.fetch_webhook_client, "fetch_async", fake_fetch
    )


def _log_rows(factory, level: str | None = None) -> list[str]:
    messages = [
        (log.level, log.message) for log in LogRepository(factory).get_recent(500)
    ]
    return [m for lvl, m in messages if level is None or lvl == level]


@pytest.mark.asyncio
async def test_a_failing_source_is_recorded_in_the_log_table(tmp_path, monkeypatch):
    """The whole point: after a failure, the durable log can say what happened."""
    factory = ConnectionFactory(tmp_path / "fail.db")
    factory.initialize_schema()
    repository = WhatsAppFetchRelayRepository(factory)
    mock_server = WhatsAppFetchLocalMockServer()
    scheduler = FetchWebhookBindingScheduler()
    service = WhatsAppFetchRelayService(
        repository, LogRepository(factory), _StubGroupSender(), mock_server, scheduler
    )
    box = {"reason": "The operation timed out"}
    _failing_fetch(monkeypatch, box)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Noteify", fetch_url="https://example.test/hook")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        warnings = _log_rows(factory, "Warning")
        assert len(warnings) == 1
        assert "Noteify" in warnings[0]
        assert "timed out" in warnings[0]
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_a_source_that_keeps_failing_is_logged_once_per_distinct_reason(tmp_path, monkeypatch):
    """A 3-second poll loop against a dead source would write ~1200 identical
    rows an hour and bury the one that explains anything. The same reason is
    recorded once; a CHANGED reason is new information and is recorded."""
    factory = ConnectionFactory(tmp_path / "repeat.db")
    factory.initialize_schema()
    repository = WhatsAppFetchRelayRepository(factory)
    mock_server = WhatsAppFetchLocalMockServer()
    scheduler = FetchWebhookBindingScheduler()
    service = WhatsAppFetchRelayService(
        repository, LogRepository(factory), _StubGroupSender(), mock_server, scheduler
    )
    box = {"reason": "The operation timed out"}
    _failing_fetch(monkeypatch, box)
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Noteify", fetch_url="https://example.test/hook")
        await service.save_binding_async(binding)

        for _ in range(5):
            await service.poll_binding_now_async(binding.binding_id)
        assert len(_log_rows(factory, "Warning")) == 1

        box["reason"] = "HTTP 502: bad gateway"
        await service.poll_binding_now_async(binding.binding_id)

        warnings = _log_rows(factory, "Warning")
        assert len(warnings) == 2
        assert any("502" in w for w in warnings)
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_recovery_after_a_failure_is_recorded_too(tmp_path, monkeypatch):
    """Without a recovery line, a log reader cannot tell a source that is still
    broken from one that fixed itself a second later."""
    factory = ConnectionFactory(tmp_path / "recover.db")
    factory.initialize_schema()
    repository = WhatsAppFetchRelayRepository(factory)
    mock_server = WhatsAppFetchLocalMockServer()
    scheduler = FetchWebhookBindingScheduler()
    service = WhatsAppFetchRelayService(
        repository, LogRepository(factory), _StubGroupSender(), mock_server, scheduler
    )
    from winspark.connectors import fetch_webhook_relay_service
    from winspark.connectors.fetch_webhook_models import WhatsAppFetchApiResult

    state = {"fail": True}

    async def flaky_fetch(fetch_url, api_key):
        if state["fail"]:
            return WhatsAppFetchApiResult.failed("The operation timed out")
        return WhatsAppFetchApiResult.blank("json-root:message")

    monkeypatch.setattr(
        fetch_webhook_relay_service.fetch_webhook_client, "fetch_async", flaky_fetch
    )
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Noteify", fetch_url="https://example.test/hook")
        await service.save_binding_async(binding)
        await service.poll_binding_now_async(binding.binding_id)

        state["fail"] = False
        await service.poll_binding_now_async(binding.binding_id)

        assert any("recovered" in m for m in _log_rows(factory, "Information"))

        # And the latch is clear: a LATER failure is recorded afresh rather than
        # being mistaken for the one already logged.
        state["fail"] = True
        await service.poll_binding_now_async(binding.binding_id)
        assert len(_log_rows(factory, "Warning")) == 2
    finally:
        scheduler.dispose()
        mock_server.stop()


@pytest.mark.asyncio
async def test_a_message_given_up_on_is_logged_as_an_error_with_its_text(tmp_path):
    """The least recoverable thing this relay can do is abandon a message. That
    row carries the text, so the message can be identified and re-sent by hand."""
    factory = ConnectionFactory(tmp_path / "gaveup.db")
    factory.initialize_schema()
    repository = WhatsAppFetchRelayRepository(factory)
    mock_server = WhatsAppFetchLocalMockServer()
    scheduler = FetchWebhookBindingScheduler()
    sender = _StubGroupSender(fail_times=99)  # never succeeds
    service = WhatsAppFetchRelayService(
        repository, LogRepository(factory), sender, mock_server, scheduler
    )
    try:
        binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="")
        await service.save_binding_async(binding)
        mock_server.inject_message("Infosys", "the payment is due friday")

        for _ in range(FetchWebhookDefaults.MAX_SEND_ATTEMPTS + 1):
            await service.poll_binding_now_async(binding.binding_id)

        errors = _log_rows(factory, "Error")
        assert len(errors) == 1
        assert "GAVE UP" in errors[0]
        assert "the payment is due friday" in errors[0]
        assert "Infosys" in errors[0]
    finally:
        scheduler.dispose()
        mock_server.stop()
