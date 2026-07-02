"""Runs the winSpark engines in a background asyncio loop, exposing a small
synchronous, thread-safe interface for the Qt UI to call.

Qt has its own event loop and can't share asyncio's, so the engines (window
discovery, event monitoring, and the fetch-webhook relay + its STA/UIA work)
run on a dedicated background thread with their own asyncio loop. The UI thread
submits coroutines via `run_coroutine_threadsafe` and reads plain SQLite / live
snapshot state directly (each read opens its own short-lived connection or reads
an immutable snapshot, both safe across threads).

This class *is* the production controller the UI depends on; tests drive the
panels with a lighter fake exposing the same method surface.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Optional

from winspark.connectors.fetch_webhook_mock_server import WhatsAppFetchLocalMockServer
from winspark.connectors.fetch_webhook_models import (
    FetchWebhookDefaults,
    WhatsAppFetchBindingEntity,
    WhatsAppFetchRelayMessageEntity,
)
from winspark.connectors.fetch_webhook_relay_service import WhatsAppFetchRelayService
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository
from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler
from winspark.connectors.fetch_webhook_url import normalize_poll_url
from winspark.constants import SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED
from winspark.data.connection import ConnectionFactory
from winspark.data.repositories import (
    ApplicationRepository,
    ApplicationSnapshotRepository,
    EventRepository,
    LogRepository,
    SettingsRepository,
)
from winspark.domain.entities import EventEntity
from winspark.domain.models import WindowInfo
from winspark.eventbus.bus import EventBus

logger = logging.getLogger(__name__)

_SUBMIT_TIMEOUT_SECONDS = 30


def _friendly_name(process_name: str, title: str, pid: int) -> str:
    return process_name.removesuffix(".exe").capitalize()


class EngineHost:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._factory = connection_factory
        self._repository = WhatsAppFetchRelayRepository(connection_factory)
        self._settings = SettingsRepository(connection_factory)
        self._event_repository = EventRepository(connection_factory)

        self._sta_manager = None
        self._scheduler = FetchWebhookBindingScheduler()
        self._mock_server = WhatsAppFetchLocalMockServer()
        self._connector = None
        self._group_sender = None

        group_sender = self._build_group_sender()
        self._relay_service = WhatsAppFetchRelayService(
            self._repository,
            LogRepository(connection_factory),
            group_sender,
            self._mock_server,
            self._scheduler,
        )

        # Observation engines (Windows only). Wired here, started on the loop.
        self._event_bus = EventBus()
        self._discovery_engine = None
        self._monitoring_engine = None
        if sys.platform == "win32":
            self._build_observation_engines()

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

    def _build_observation_engines(self) -> None:
        try:
            from winspark.engines.event_monitoring import EventMonitoringEngine
            from winspark.engines.window_discovery import WindowDiscoveryEngine

            self._discovery_engine = WindowDiscoveryEngine(name_formatter=_friendly_name)
            self._monitoring_engine = EventMonitoringEngine(
                discovery_engine=self._discovery_engine,
                event_repository=self._event_repository,
                application_repository=ApplicationRepository(self._factory),
                snapshot_repository=ApplicationSnapshotRepository(self._factory),
                event_bus=self._event_bus,
            )
        except Exception as ex:  # noqa: BLE001
            logger.warning("Window observation unavailable (%s)", ex)
            self._discovery_engine = None
            self._monitoring_engine = None

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="winSpark-engine-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

        if self._monitoring_engine is not None and self._discovery_engine is not None:
            try:
                self._submit(self._monitoring_engine.start())
                self._submit(self._discovery_engine.start())
            except Exception:  # noqa: BLE001
                logger.warning("Failed to start observation engines", exc_info=True)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def shutdown(self) -> None:
        if self._loop is not None and self._loop.is_running():
            for coro in self._safe_stop_coros():
                try:
                    self._submit(coro)
                except Exception:  # noqa: BLE001
                    pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._scheduler.dispose()
        self._mock_server.stop()
        if self._sta_manager is not None:
            self._sta_manager.dispose()

    def _safe_stop_coros(self):
        yield self._relay_service.set_relay_enabled_async(False)
        if self._discovery_engine is not None:
            yield self._discovery_engine.stop()
        if self._monitoring_engine is not None:
            yield self._monitoring_engine.stop()

    def _submit(self, coro, timeout: float = _SUBMIT_TIMEOUT_SECONDS):
        assert self._loop is not None, "EngineHost.start() must be called first"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # --- reads (Qt thread) ----------------------------------------------

    def get_bindings(self) -> list[WhatsAppFetchBindingEntity]:
        return self._repository.get_bindings()

    def get_recent_messages(self, limit: int = 30) -> list[WhatsAppFetchRelayMessageEntity]:
        return self._repository.get_recent_messages(limit)

    def is_relay_enabled(self) -> bool:
        value = self._settings.get_value(SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED)
        return value is not None and value.lower() in ("true", "1")

    def get_windows(self) -> list[WindowInfo]:
        if self._discovery_engine is None or self._discovery_engine.current_snapshot is None:
            return []
        return list(self._discovery_engine.current_snapshot.windows)

    def get_recent_events(self, limit: int = 100) -> list[EventEntity]:
        return self._event_repository.get_recent(limit)

    def get_whatsapp_chats(self) -> Optional[list]:
        """Returns WhatsAppChatRow objects (name + unread + preview), or None if
        WhatsApp integration isn't available on this platform."""
        if self._connector is None:
            return None
        try:
            handle = self._submit(self._connector.find_window_async())
            if handle is None:
                return []
            return list(self._submit(self._connector.read_chat_rows_async(handle)))
        except Exception:  # noqa: BLE001
            logger.warning("get_whatsapp_chats failed", exc_info=True)
            return []

    def is_whatsapp_running(self) -> bool:
        if self._connector is None:
            return False
        try:
            return self._submit(self._connector.find_window_async()) is not None
        except Exception:  # noqa: BLE001
            return False

    def list_chats(self) -> Optional[list[str]]:
        rows = self.get_whatsapp_chats()
        return None if rows is None else [r.chat_name for r in rows]

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

    def send_to_chat(self, group: str, text: str) -> tuple[bool, str]:
        """Send a message to a WhatsApp chat right now (drives the real UI).
        Returns (success, status_or_reason)."""
        if self._group_sender is None:
            return False, "WhatsApp sending is not available on this platform."
        try:
            result = self._submit(self._group_sender.send_to_group_async(group, text), timeout=120)
        except Exception as ex:  # noqa: BLE001
            return False, str(ex)
        return (result.success, result.status if result.success else result.failure_reason)


class _NoopGroupSender:
    """Stand-in sender used off-Windows so the UI still manages bindings and
    shows history; every send reports a clear failure rather than pretending."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def send_to_group_async(self, group_name: str, message_text: str):
        from winspark.connectors.fetch_webhook_models import WhatsAppGroupSendResult

        return WhatsAppGroupSendResult.failed(f"Sending unavailable: {self._reason}")
