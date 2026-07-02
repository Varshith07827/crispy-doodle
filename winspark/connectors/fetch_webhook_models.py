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


class FetchWebhookDefaults:
    MOCK_PORT = 5001
    MIN_POLL_INTERVAL_SECONDS = 3
    DEFAULT_POLL_INTERVAL_SECONDS = 3
    MAX_SEND_ATTEMPTS = 3
    RETRY_DELAY_SECONDS = 30

    @staticmethod
    def mock_url_for_group(group_name: str) -> str:
        slug = quote(group_name.strip())
        return f"http://localhost:{FetchWebhookDefaults.MOCK_PORT}/webhook/{slug}"

    @staticmethod
    def mock_inject_url_for_group(group_name: str) -> str:
        """POST here to queue test messages (not the poll URL)."""
        slug = quote(group_name.strip())
        return f"http://localhost:{FetchWebhookDefaults.MOCK_PORT}/api/inject/{slug}"

    @staticmethod
    def mock_shared_inject_url() -> str:
        """POST here to queue messages across all enabled bindings, round-robin."""
        return f"http://localhost:{FetchWebhookDefaults.MOCK_PORT}/api/inject"


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
