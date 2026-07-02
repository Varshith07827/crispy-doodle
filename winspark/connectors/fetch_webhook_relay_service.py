"""Port of WinSpark.Infrastructure.Services.WhatsApp.WhatsAppFetchRelayService.

The orchestrator: poll each enabled binding's URL on its own schedule, parse
the response, dedupe against what's already been sent (by external id or
content hash), persist it, and hand it to the group sender — with retry on
failure up to MaxSendAttempts.

group_sender is any object exposing an async `send_to_group_async(group_name,
message_text) -> WhatsAppGroupSendResult` — in production that's
WhatsAppGroupSender (winspark/connectors/whatsapp_group_sender.py); tests use
a stub instead, since exercising a real send would actually deliver a message
to a real contact (see PORT_NOTES.md).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from winspark.connectors import fetch_webhook_client
from winspark.connectors.fetch_webhook_models import (
    FetchWebhookDefaults,
    WhatsAppFetchBindingEntity,
    WhatsAppFetchRelayMessageEntity,
    WhatsAppFetchRelayMessageState,
    WhatsAppGroupSendResult,
)
from winspark.connectors.fetch_webhook_mock_server import WhatsAppFetchLocalMockServer
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository, compute_content_hash
from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler
from winspark.connectors.fetch_webhook_url import normalize_poll_url
from winspark.data.repositories import LogRepository
from winspark.domain.entities import LogEntity

logger = logging.getLogger(__name__)


class GroupSender(Protocol):
    async def send_to_group_async(self, group_name: str, message_text: str) -> WhatsAppGroupSendResult: ...


class WhatsAppFetchRelayService:
    def __init__(
        self,
        repository: WhatsAppFetchRelayRepository,
        log_repository: LogRepository,
        group_sender: GroupSender,
        local_mock: WhatsAppFetchLocalMockServer,
        scheduler: FetchWebhookBindingScheduler,
    ) -> None:
        self._repository = repository
        self._log_repository = log_repository
        self._group_sender = group_sender
        self._local_mock = local_mock
        self._scheduler = scheduler
        self._relay_enabled = False
        self._last_tick_hint: Optional[str] = None
        self._status_changed_handlers: list[Callable[[], None]] = []
        self._relay_cycle_lock = asyncio.Lock()
        self._retry_sweep_lock = asyncio.Lock()

        scheduler.set_binding_poll_requested_handler(self._on_binding_poll_requested)

    @property
    def is_relay_enabled(self) -> bool:
        return self._relay_enabled

    @property
    def last_tick_hint(self) -> Optional[str]:
        return self._last_tick_hint

    def on_status_changed(self, handler: Callable[[], None]) -> None:
        self._status_changed_handlers.append(handler)

    def _notify_status_changed(self) -> None:
        for handler in self._status_changed_handlers:
            handler()

    async def set_relay_enabled_async(self, enabled: bool) -> None:
        self._relay_enabled = enabled
        self._scheduler.set_relay_enabled(enabled)
        logger.info("Fetch-Webhook relay %s", "enabled" if enabled else "disabled")

        if enabled:
            self._ensure_local_mock_for_bindings()
            await self.sync_scheduler_async()
            for binding in self._repository.get_bindings():
                if binding.is_enabled:
                    await self.poll_binding_async(binding.binding_id)
        else:
            self._scheduler.sync_bindings([])

        self._notify_status_changed()

    def get_bindings(self) -> list[WhatsAppFetchBindingEntity]:
        return self._repository.get_bindings()

    async def save_binding_async(self, binding: WhatsAppFetchBindingEntity) -> None:
        normalized_url = normalize_poll_url(binding.fetch_url, binding.group_name)
        if normalized_url != binding.fetch_url:
            binding = replace(binding, fetch_url=normalized_url)

        self._repository.upsert_binding(binding)
        await self.sync_scheduler_async()
        self._ensure_local_mock_for_bindings()
        self._notify_status_changed()

    async def delete_binding_async(self, binding_id: str) -> None:
        self._scheduler.stop_binding(binding_id)
        self._repository.delete_binding(binding_id)
        await self.sync_scheduler_async()
        self._notify_status_changed()

    async def pause_binding_async(self, binding_id: str) -> None:
        self._repository.set_binding_enabled(binding_id, False)
        self._repository.update_binding_status(binding_id, "paused")
        await self.sync_scheduler_async()
        self._notify_status_changed()

    async def resume_binding_async(self, binding_id: str) -> None:
        self._repository.set_binding_enabled(binding_id, True)
        self._repository.update_binding_status(binding_id, "blank")
        await self.sync_scheduler_async()

        binding = self._repository.get_binding(binding_id)
        if binding is not None and self._relay_enabled:
            self._scheduler.start_or_update(binding.binding_id, binding.poll_interval_seconds)
            await self._process_retries_for_binding(binding)
            await self.poll_binding_async(binding_id)
            self._last_tick_hint = f"Resumed {binding.group_name.strip()} — polling active."

        self._notify_status_changed()

    async def poll_binding_now_async(self, binding_id: str) -> None:
        await self.poll_binding_async(binding_id, manual=True)

    def get_recent_messages(self, limit: int = 50) -> list[WhatsAppFetchRelayMessageEntity]:
        return self._repository.get_recent_messages(limit)

    async def inject_test_message_async(self, group_name: str, message_text: str) -> None:
        self._ensure_local_mock_for_bindings()
        lines = [line.strip() for line in message_text.replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            return

        for line in lines:
            self._local_mock.inject_message(group_name, line)

        queued = self._local_mock.get_queued_count(group_name)
        self._last_tick_hint = (
            f"Queued 1 message for {group_name.strip()} ({queued} pending) — next poll will relay it."
            if len(lines) == 1
            else f"Queued {len(lines)} messages for {group_name.strip()} ({queued} pending) — one per poll."
        )
        self._notify_status_changed()

    async def sync_scheduler_async(self) -> None:
        bindings = self._repository.get_bindings()
        self._scheduler.sync_bindings(bindings if self._relay_enabled else [])
        self._ensure_local_mock_for_bindings()

    async def trigger_poll_now_async(self) -> None:
        for binding in self._repository.get_bindings():
            if binding.is_enabled:
                await self.poll_binding_async(binding.binding_id)

    async def process_tick_async(self) -> None:
        await self._process_retries()

    async def poll_binding_async(self, binding_id: str, manual: bool = False) -> None:
        if not self._relay_enabled and not manual:
            return

        async with self._relay_cycle_lock:
            binding = self._repository.get_binding(binding_id)
            if binding is None or not binding.is_enabled:
                return

            try:
                self._ensure_local_mock_for_bindings()
                await self._process_retries_for_binding(binding)
                await self._poll_binding_fetch(binding)
            except Exception:  # noqa: BLE001
                logger.warning("Fetch-Webhook poll failed for binding %s", binding_id, exc_info=True)

    async def _on_binding_poll_requested(self, binding_id: str) -> None:
        await self.poll_binding_async(binding_id)

    async def _process_retries(self) -> None:
        if not self._relay_enabled or self._retry_sweep_lock.locked():
            return

        async with self._retry_sweep_lock:
            for message in self._repository.get_retryable_messages():
                binding = self._repository.get_binding(message.binding_id)
                if binding is not None and binding.is_enabled:
                    await self._send_stored_message(binding, message)

    async def _process_retries_for_binding(self, binding: WhatsAppFetchBindingEntity) -> None:
        for message in self._repository.get_retryable_messages():
            if message.binding_id == binding.binding_id:
                await self._send_stored_message(binding, message)

    async def _poll_binding_fetch(self, binding: WhatsAppFetchBindingEntity) -> None:
        binding = self._ensure_binding_poll_url(binding)
        self._repository.increment_binding_poll_count(binding.binding_id)

        fetch = await fetch_webhook_client.fetch_async(binding.fetch_url, binding.api_key)
        now = datetime.now(timezone.utc)

        if fetch.is_error:
            self._repository.update_binding_status(binding.binding_id, "error", last_fetch_utc=now, last_error=fetch.error_message or "Fetch error")
            self._notify_status_changed()
            return

        if not fetch.has_message or not (fetch.message or "").strip():
            self._repository.update_binding_status(binding.binding_id, "blank", last_fetch_utc=now)
            self._notify_status_changed()
            return

        content_hash = compute_content_hash(fetch.message)
        existing = self._repository.find_message(binding.binding_id, fetch.external_id, content_hash)

        if existing is not None:
            if existing.state in (
                WhatsAppFetchRelayMessageState.PENDING,
                WhatsAppFetchRelayMessageState.FAILED,
                WhatsAppFetchRelayMessageState.RETRYING,
                WhatsAppFetchRelayMessageState.SENDING,
            ):

                await self._send_stored_message(binding, replace(existing, message_text=fetch.message))
                return

            if existing.state == WhatsAppFetchRelayMessageState.SENT:
                if fetch.external_id and existing.external_id == fetch.external_id:
                    self._repository.update_binding_status(
                        binding.binding_id, "duplicate", last_fetch_utc=now, last_error=f"Already sent (external id {fetch.external_id})"
                    )
                    self._notify_status_changed()
                    return
                # same text fetched again without an external id to distinguish — re-deliver

        if self._repository.has_in_flight_message(binding.binding_id, fetch.external_id, content_hash):
            in_flight = self._repository.find_message(binding.binding_id, fetch.external_id, content_hash)
            if in_flight is not None and in_flight.state in (
                WhatsAppFetchRelayMessageState.PENDING,
                WhatsAppFetchRelayMessageState.SENDING,
            ):

                await self._send_stored_message(binding, replace(in_flight, message_text=fetch.message))
                return

            self._repository.update_binding_status(binding.binding_id, "duplicate", last_fetch_utc=now, last_error="Same message already being processed")
            self._notify_status_changed()
            return

        stored = WhatsAppFetchRelayMessageEntity(
            binding_id=binding.binding_id,
            external_id=fetch.external_id,
            message_text=fetch.message,
            content_hash=content_hash,
            state=WhatsAppFetchRelayMessageState.PENDING,
            fetch_utc=now,
            parse_strategy=fetch.parse_strategy,
        )
        self._repository.insert_message(stored)
        self._repository.update_binding_status(binding.binding_id, "message", last_fetch_utc=now, last_message_received_utc=now)

        logger.info("Fetch-Webhook stored message for %s via %s (%d chars)", binding.group_name, fetch.parse_strategy, len(fetch.message))
        await self._send_stored_message(binding, stored)

    async def _send_stored_message(self, binding: WhatsAppFetchBindingEntity, message: WhatsAppFetchRelayMessageEntity) -> None:

        current = self._repository.get_message_by_id(message.message_id)
        if current is None:
            return
        if current.state in (WhatsAppFetchRelayMessageState.SENT, WhatsAppFetchRelayMessageState.SENDING):
            return
        if current.attempt_count >= FetchWebhookDefaults.MAX_SEND_ATTEMPTS:
            return

        message_text = message.message_text.strip() or current.message_text
        message = replace(current, message_text=message_text)

        attempt = message.attempt_count + 1
        sending = replace(message, state=WhatsAppFetchRelayMessageState.SENDING, attempt_count=attempt, next_retry_utc=None)
        self._repository.update_message(sending)
        self._repository.update_binding_status(binding.binding_id, "sending")
        self._notify_status_changed()

        result = await self._group_sender.send_to_group_async(binding.group_name, message.message_text)

        if result.success:
            sent_utc = datetime.now(timezone.utc)
            self._repository.update_message(replace(sending, state=WhatsAppFetchRelayMessageState.SENT, sent_utc=sent_utc, last_error=""))
            self._repository.increment_binding_sent_count(binding.binding_id, sent_utc)
            state = "sent-verified" if result.verified else "sent-unverified"
            self._repository.update_binding_status(binding.binding_id, state, last_send_utc=sent_utc)

            self._log_repository.insert(
                LogEntity(
                    level="Information",
                    source="WhatsAppFetchRelayService",
                    message=f"Fetch-Webhook sent to {binding.group_name}: {_truncate(message.message_text, 120)}",
                    timestamp_utc=sent_utc,
                )
            )
            logger.info("Fetch-Webhook sent message to %s", binding.group_name)
            self._notify_status_changed()
            return

        reason = result.failure_reason
        logger.warning("Fetch-Webhook send failed for %s (attempt %d): %s", binding.group_name, attempt, reason)

        if attempt >= FetchWebhookDefaults.MAX_SEND_ATTEMPTS:
            self._repository.update_message(replace(sending, state=WhatsAppFetchRelayMessageState.FAILED, last_error=reason))
            self._repository.update_binding_status(binding.binding_id, "send-failed", last_error=reason)
        else:
            next_retry = datetime.now(timezone.utc).timestamp() + FetchWebhookDefaults.RETRY_DELAY_SECONDS
            next_retry_dt = datetime.fromtimestamp(next_retry, tz=timezone.utc)
            self._repository.update_message(
                replace(sending, state=WhatsAppFetchRelayMessageState.RETRYING, last_error=reason, next_retry_utc=next_retry_dt)
            )
            self._repository.update_binding_status(binding.binding_id, "retrying", last_error=reason)

        self._notify_status_changed()

    def _ensure_local_mock_for_bindings(self) -> None:
        self._local_mock.ensure_started(FetchWebhookDefaults.MOCK_PORT)
        groups = [b.group_name.strip() for b in self._repository.get_bindings() if b.is_enabled and b.group_name.strip()]
        self._local_mock.configure_round_robin_groups(groups)

    def _ensure_binding_poll_url(self, binding: WhatsAppFetchBindingEntity) -> WhatsAppFetchBindingEntity:
        normalized = normalize_poll_url(binding.fetch_url, binding.group_name)
        if normalized == binding.fetch_url:
            return binding


        fixed = replace(binding, fetch_url=normalized)
        self._repository.upsert_binding(fixed)
        return fixed


def _truncate(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else value[:max_len] + "…"
