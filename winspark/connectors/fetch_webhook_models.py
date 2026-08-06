"""Port of WinSpark.Domain.{Entities.WhatsAppFetchRelayEntities, Models.Connectors.FetchWebhookModels}.

The "Fetch-Webhook" feature: bind a WhatsApp chat to an external GET URL
(typically backed by an AI service), poll it on an interval, and relay any
non-empty response into that chat. This is the app's actual "AI" integration
point — winSpark doesn't call an LLM itself, it relays whatever an external
service returns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Optional
from urllib.parse import quote


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WhatsAppFetchRelayMessageState(IntEnum):
    PENDING = 0  # "Read" in the .NET comment
    SENDING = 1
    SENT = 2
    FAILED = 3
    RETRYING = 4


@dataclass(slots=True)
class WhatsAppFetchBindingEntity:
    """One WhatsApp chat bound to one GET API endpoint."""

    binding_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    group_name: str = ""
    fetch_url: str = ""
    api_key: str = ""
    poll_interval_seconds: int = 60
    is_enabled: bool = True
    # Which reply source this chat uses: "web" = poll fetch_url (the original
    # Fetch-Webhook / built-in test source), "openai" = winSpark calls OpenAI,
    # "trigger" = watch for a message matching trigger_text and send reply_text.
    reply_source: str = "web"
    # For reply_source == "openai": "reply" = respond to the newest incoming
    # message, "generate" = produce a message from ai_prompt on each check,
    # "command" = respond ONLY to messages that address the bot by name
    # ("!winspark what's the weather"), with the name held in trigger_text.
    ai_mode: str = "reply"
    # Per-chat OpenAI instructions (the system prompt). The API key/model are
    # app-wide (Settings), not stored here.
    ai_prompt: str = ""
    # Two uses, depending on the mode above:
    #   reply_source == "trigger"  -> the phrase to wait for (matched by meaning
    #     when OpenAI is configured, else by words), answered with reply_text.
    #   ai_mode == "command"       -> the bot's name, matched EXACTLY after the
    #     command prefix and answered by the AI. Deliberately the same column:
    #     both are "the thing to watch the chat for", and reusing it keeps the
    #     command mode free of a schema migration.
    trigger_text: str = ""
    reply_text: str = ""
    last_fetch_utc: Optional[datetime] = None
    last_fetch_state: str = ""
    last_message_received_utc: Optional[datetime] = None
    last_send_utc: Optional[datetime] = None
    total_polls: int = 0
    total_sent: int = 0
    last_error: str = ""
    created_at_utc: datetime = field(default_factory=_utcnow)
    updated_at_utc: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class WhatsAppFetchRelayMessageEntity:
    """A message read from the webhook, persisted immediately after fetch."""

    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    binding_id: str = ""
    external_id: Optional[str] = None
    message_text: str = ""
    content_hash: str = ""
    state: WhatsAppFetchRelayMessageState = WhatsAppFetchRelayMessageState.PENDING
    fetch_utc: datetime = field(default_factory=_utcnow)
    sent_utc: Optional[datetime] = None
    next_retry_utc: Optional[datetime] = None
    parse_strategy: str = ""
    last_error: str = ""
    attempt_count: int = 0
    # The chat-memory key this message belongs under, resolved and stored before
    # the send (resolving it needs WhatsApp open, which a later restart may not
    # have). Empty on rows written before this existed.
    memory_key: str = ""
    # When chat memory was confirmed to hold this message. None on a SENT row
    # means delivered-but-not-remembered — the gap a crash between the two
    # writes leaves behind, and what repair_unremembered_sends_async fixes.
    memory_recorded_utc: Optional[datetime] = None


class FetchWebhookDefaults:
    MOCK_PORT = 5001
    MIN_POLL_INTERVAL_SECONDS = 3
    DEFAULT_POLL_INTERVAL_SECONDS = 3
    MAX_SEND_ATTEMPTS = 3
    RETRY_DELAY_SECONDS = 30

    # Per-chat memory for AI replies. Two tiers so replies have context without
    # ever sending a whole history to the provider:
    #   * RECENT — the tail of the conversation, always included verbatim so the
    #     AI follows the immediate thread.
    #   * ARCHIVE — a larger retained store the RAG retriever searches to pull in
    #     the FEW older messages relevant to the current question.
    # CHAT_MEMORY_MESSAGES stays the recent-window size (kept name for callers).
    CHAT_MEMORY_MESSAGES = 15
    CHAT_MEMORY_ARCHIVE = 400
    # How many relevant older messages RAG adds to a reply prompt on top of the
    # recent window — a hard cap so the prompt stays small no matter how big the
    # archive grows.
    RAG_TOP_K = 6
    # How far back RAG searches for relevant messages. The store itself is
    # unbounded on MongoDB (the whole history is kept), but scanning/embedding
    # every message on each reply would get slow/expensive on a very long chat —
    # so retrieval considers the most recent this-many messages, which is deep
    # enough to cover months of conversation.
    RAG_SEARCH_LIMIT = 2000
    # Max texts per embeddings API request (providers cap batch size); larger
    # candidate sets are embedded in chunks of this size.
    EMBED_BATCH_SIZE = 256

    # 127.0.0.1, NOT "localhost": on Windows, "localhost" resolves to IPv6 ::1
    # first, and connecting to it fails over to IPv4 only after a ~2s timeout —
    # measured live, every localhost GET took ~2067ms vs ~15ms for 127.0.0.1.
    # That delay hit both the relay's own poll and the user's POST. Using the
    # literal IPv4 loopback keeps the inbox link instant.
    MOCK_HOST = "127.0.0.1"

    @staticmethod
    def mock_url_for_group(group_name: str) -> str:
        slug = quote(group_name.strip())
        return f"http://{FetchWebhookDefaults.MOCK_HOST}:{FetchWebhookDefaults.MOCK_PORT}/webhook/{slug}"

    @staticmethod
    def mock_inject_url_for_group(group_name: str) -> str:
        """POST here to queue test messages (not the poll URL)."""
        slug = quote(group_name.strip())
        return f"http://{FetchWebhookDefaults.MOCK_HOST}:{FetchWebhookDefaults.MOCK_PORT}/api/inject/{slug}"

    @staticmethod
    def mock_shared_inject_url() -> str:
        """POST here to queue messages across all enabled bindings, round-robin."""
        return f"http://{FetchWebhookDefaults.MOCK_HOST}:{FetchWebhookDefaults.MOCK_PORT}/api/inject"


@dataclass(frozen=True, slots=True)
class WhatsAppFetchApiResult:
    is_error: bool = False
    has_message: bool = False
    message: Optional[str] = None
    external_id: Optional[str] = None
    error_message: Optional[str] = None
    parse_strategy: str = ""

    @staticmethod
    def blank(strategy: str = "empty") -> "WhatsAppFetchApiResult":
        return WhatsAppFetchApiResult(has_message=False, parse_strategy=strategy)

    @staticmethod
    def with_message(message: str, external_id: Optional[str], strategy: str = "plain-text") -> "WhatsAppFetchApiResult":
        return WhatsAppFetchApiResult(has_message=True, message=message, external_id=external_id, parse_strategy=strategy)

    @staticmethod
    def failed(error_message: str) -> "WhatsAppFetchApiResult":
        return WhatsAppFetchApiResult(is_error=True, error_message=error_message)


@dataclass(frozen=True, slots=True)
class WebhookProbeResult:
    ok: bool = False
    status_code: int = 0
    message: str = ""

    @staticmethod
    def ok_result(status_code: int, message: str) -> "WebhookProbeResult":
        return WebhookProbeResult(ok=True, status_code=status_code, message=message)

    @staticmethod
    def failed(status_code: int, message: str) -> "WebhookProbeResult":
        return WebhookProbeResult(ok=False, status_code=status_code, message=message)


@dataclass(frozen=True, slots=True)
class WhatsAppGroupSendResult:
    success: bool = False
    failure_reason: str = ""
    verified: bool = False
    message_appeared: bool = False
    status: str = ""

    @staticmethod
    def succeeded(status: str, verified: bool, appeared: bool) -> "WhatsAppGroupSendResult":
        return WhatsAppGroupSendResult(success=True, status=status, verified=verified, message_appeared=appeared)

    @staticmethod
    def failed(reason: str) -> "WhatsAppGroupSendResult":
        return WhatsAppGroupSendResult(success=False, failure_reason=reason, status=reason)
