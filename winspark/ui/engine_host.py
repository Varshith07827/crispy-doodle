"""Runs the Fetch-Webhook relay engine in a background asyncio loop, exposing a
small synchronous, thread-safe interface for the Qt UI to call.

Qt has its own event loop and can't share asyncio's, so the async engine (relay
service + scheduler + STA/UIA work) runs on a dedicated background thread with
its own asyncio loop. The UI thread submits coroutines via
`run_coroutine_threadsafe` and reads plain SQLite state directly (each read
opens its own short-lived connection, which is safe across threads).

This class *is* the production `RelayController` the UI depends on; tests use a
lighter fake with the same method surface (see tests/test_ui_main_window.py).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

from winspark.connectors.fetch_webhook_mock_server import WhatsAppFetchLocalMockServer
from winspark.connectors.fetch_webhook_models import FetchWebhookDefaults, WhatsAppFetchBindingEntity, WhatsAppFetchRelayMessageEntity
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository
from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler
from winspark.connectors.fetch_webhook_relay_service import WhatsAppFetchRelayService
from winspark.connectors.fetch_webhook_url import normalize_poll_url
from winspark.constants import SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED
from winspark.data.connection import ConnectionFactory
from winspark.data.repositories import LogRepository, SettingsRepository

logger = logging.getLogger(__name__)

_SUBMIT_TIMEOUT_SECONDS = 30


class EngineHost:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._factory = connection_factory
        self._repository = WhatsAppFetchRelayRepository(connection_factory)
        self._settings = SettingsRepository(connection_factory)

        self._sta_manager = None
        self._scheduler = FetchWebhookBindingScheduler()
        self._mock_server = WhatsAppFetchLocalMockServer()
        self._connector = None
        self._group_sender = None

        # On Windows, use the real WhatsApp group sender; elsewhere (or if the
        # UIA stack is unavailable) fall back to a no-op sender so the UI still
        # runs for binding management + monitoring.
        group_sender = self._build_group_sender()
        self._relay_service = WhatsAppFetchRelayService(
            self._repository,
            LogRepository(connection_factory),
            group_sender,
            self._mock_server,
            self._scheduler,
        )

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def _build_group_sender(self):
        if sys.platform != "win32":
            return _NoopGroupSender("group send is only available on Windows")
        try:
            from winspark.automation.sta_thread_manager import StaAutomationThreadManager
            from winspark.connectors.whatsapp import WhatsAppConnector
            from winspark.connectors.whatsapp_group_sender import WhatsAppGroupSender

            self._sta_manager = StaAutomationThreadManager()
            self._connector = WhatsAppConnector(self._sta_manager)
            self._group_sender = WhatsAppGroupSender(self._connector, self._sta_manager)
            return self._group_sender
        except Exception as ex:  # noqa: BLE001 - missing pywin32/uiautomation
            logger.warning("WhatsApp sending unavailable (%s); using no-op sender", ex)
            return _NoopGroupSender(str(ex))

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="winSpark-relay-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def shutdown(self) -> None:
        if self._loop is not None and self._loop.is_running():
            try:
                self._submit(self._relay_service.set_relay_enabled_async(False))
            except Exception:  # noqa: BLE001
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._scheduler.dispose()
        self._mock_server.stop()
        if self._sta_manager is not None:
            self._sta_manager.dispose()

    def _submit(self, coro, timeout: float = _SUBMIT_TIMEOUT_SECONDS):
        assert self._loop is not None, "EngineHost.start() must be called first"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # --- reads (called from the Qt thread; direct SQLite, no loop needed) ---

    def get_bindings(self) -> list[WhatsAppFetchBindingEntity]:
        return self._repository.get_bindings()

    def get_recent_messages(self, limit: int = 30) -> list[WhatsAppFetchRelayMessageEntity]:
        return self._repository.get_recent_messages(limit)

    def is_relay_enabled(self) -> bool:
        value = self._settings.get_value(SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED)
        return value is not None and value.lower() in ("true", "1")

    # --- writes (submitted to the engine loop) --------------------------

    def set_relay_enabled(self, enabled: bool) -> None:
        self._settings.set_value(SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED, "true" if enabled else "false")
        self._submit(self._relay_service.set_relay_enabled_async(enabled))

    def add_or_update_binding(self, group: str, url: str, interval: int, api_key: str = "", enabled: bool = True) -> None:
        group = group.strip()
        existing = next((b for b in self._repository.get_bindings() if b.group_name.strip().lower() == group.lower()), None)
        binding = WhatsAppFetchBindingEntity(
            binding_id=existing.binding_id if existing else WhatsAppFetchBindingEntity().binding_id,
            group_name=group,
            fetch_url=normalize_poll_url(url, group),
            api_key=api_key,
            poll_interval_seconds=max(FetchWebhookDefaults.MIN_POLL_INTERVAL_SECONDS, interval),
            is_enabled=enabled,
        )
        self._submit(self._relay_service.save_binding_async(binding))

    def set_binding_enabled(self, binding_id: str, enabled: bool) -> None:
        if enabled:
            self._submit(self._relay_service.resume_binding_async(binding_id))
        else:
            self._submit(self._relay_service.pause_binding_async(binding_id))

    def delete_binding(self, binding_id: str) -> None:
        self._submit(self._relay_service.delete_binding_async(binding_id))

    def inject_test_message(self, group: str, text: str) -> None:
        self._submit(self._relay_service.inject_test_message_async(group, text))

    def list_chats(self) -> Optional[list[str]]:
        if self._connector is None:
            return None
        try:
            handle = self._submit(self._connector.find_window_async())
            if handle is None:
                return []
            rows = self._submit(self._connector.read_chat_rows_async(handle))
            return [r.chat_name for r in rows]
        except Exception:  # noqa: BLE001
            logger.warning("list_chats failed", exc_info=True)
            return []


class _NoopGroupSender:
    """Stand-in sender used off-Windows so the UI still manages bindings and
    shows history; every send reports a clear failure rather than pretending."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def send_to_group_async(self, group_name: str, message_text: str):
        from winspark.connectors.fetch_webhook_models import WhatsAppGroupSendResult

        return WhatsAppGroupSendResult.failed(f"Sending unavailable: {self._reason}")
