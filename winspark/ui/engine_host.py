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
from collections import deque
from datetime import datetime, timezone
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
from winspark.constants import (
    DEFAULT_AI_PROVIDER,
    SETTINGS_AI_PROVIDER,
    SETTINGS_OPENAI_API_KEY,
    SETTINGS_OPENAI_MODEL,
    SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED,
    ai_provider_info,
)
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
from winspark.ui.activity import describe_activity
from winspark.ui.apps import RunningApp, detect_running_apps

logger = logging.getLogger(__name__)

_SUBMIT_TIMEOUT_SECONDS = 30
_ACTIVITY_LOG_CAPACITY = 500


def _friendly_name(process_name: str, title: str, pid: int) -> str:
    return process_name.removesuffix(".exe").capitalize()


def _is_localhost(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url.strip()).hostname or "").lower()
    return host in ("localhost", "127.0.0.1")


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
            openai_config_provider=self._read_openai_config,
        )

        # Plain-English activity log, fed by the relay's neutral activity events.
        self._activity: deque = deque(maxlen=_ACTIVITY_LOG_CAPACITY)
        self._activity_lock = threading.Lock()
        self._relay_service.on_activity(self._on_activity)

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

        # Resume automation if it was on when the app last closed. Without this
        # the persisted flag said "on" (and the status bar showed it) while the
        # relay service itself was never enabled — so nothing ever polled, and
        # "Start automation" skipped enabling it because the flag already read
        # true. The flag is the boot preference; the service is the truth.
        if self._read_relay_flag():
            try:
                self._submit(self._relay_service.set_relay_enabled_async(True))
            except Exception:  # noqa: BLE001
                logger.warning("Failed to resume automation from saved state", exc_info=True)

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
        """Whether automation is actually running — the live service state, not
        the persisted flag (a stale flag once made the UI say "on" while nothing
        polled)."""
        return self._relay_service.is_relay_enabled

    def _read_relay_flag(self) -> bool:
        value = self._settings.get_value(SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED)
        return value is not None and value.lower() in ("true", "1")

    def get_windows(self) -> list[WindowInfo]:
        if self._discovery_engine is None or self._discovery_engine.current_snapshot is None:
            return []
        return list(self._discovery_engine.current_snapshot.windows)

    def get_running_apps(self) -> list[RunningApp]:
        """The deduplicated list of recognizable running apps for the sidebar."""
        return detect_running_apps(self.get_windows())

    def get_activity_log(self, limit: int = 200) -> list[tuple[datetime, str]]:
        """Plain-English activity, newest first."""
        with self._activity_lock:
            items = list(self._activity)
        return list(reversed(items))[:limit]

    def _on_activity(self, chat: str, kind: str, detail: str) -> None:
        text = describe_activity(chat, kind, detail)
        with self._activity_lock:
            # Collapse consecutive duplicates (e.g. repeated "Checking…" ticks).
            if self._activity and self._activity[-1][1] == text:
                return
            self._activity.append((datetime.now(timezone.utc), text))

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

    def add_or_update_binding(
        self,
        group: str,
        url: str,
        interval: int,
        api_key: str = "",
        enabled: bool = True,
        reply_source: str = "web",
        ai_mode: str = "reply",
        ai_prompt: str = "",
        trigger_text: str = "",
        reply_text: str = "",
    ) -> None:
        group = group.strip()
        existing = next((b for b in self._repository.get_bindings() if b.group_name.strip().lower() == group.lower()), None)
        # Only "web" bindings poll a URL; the rest leave fetch_url as given (empty).
        fetch_url = normalize_poll_url(url, group) if reply_source == "web" else url
        binding = WhatsAppFetchBindingEntity(
            binding_id=existing.binding_id if existing else WhatsAppFetchBindingEntity().binding_id,
            group_name=group,
            fetch_url=fetch_url,
            api_key=api_key,
            poll_interval_seconds=max(FetchWebhookDefaults.MIN_POLL_INTERVAL_SECONDS, interval),
            is_enabled=enabled,
            reply_source=reply_source,
            ai_mode=ai_mode,
            ai_prompt=ai_prompt,
            trigger_text=trigger_text,
            reply_text=reply_text,
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
            return False, "Sending isn't available on this device."
        try:
            result = self._submit(self._group_sender.send_to_group_async(group, text), timeout=120)
        except Exception as ex:  # noqa: BLE001
            return False, str(ex)
        return (result.success, result.status if result.success else result.failure_reason)

    def open_chat(self, group: str) -> bool:
        """Open a chat in WhatsApp (foregrounds it once) so the live message
        view can show it. Returns whether it opened."""
        if self._group_sender is None:
            return False
        try:
            return bool(self._submit(self._group_sender.open_chat_async(group), timeout=60))
        except Exception:  # noqa: BLE001
            logger.warning("open_chat failed", exc_info=True)
            return False

    def ocr_available(self) -> bool:
        from winspark.connectors import window_ocr

        return window_ocr.is_available()

    def read_screen_text(self, window_handle: int) -> tuple[bool, str]:
        """Read the visible text on a window using Windows OCR. Returns
        (ok, text-or-plain-error). Works for any app, adapter or not."""
        from winspark.connectors import window_ocr

        result = window_ocr.read_window_text(window_handle)
        return (result.ok, result.text if result.ok else result.error)

    def ask_about_screen(self, window_handle: int, question: str) -> tuple[bool, str]:
        """Answer a question about what's on an app's window: capture + OCR the
        window, then ask the configured AI service with the screen text as
        context. Returns (ok, answer-or-plain-error). This is the Comet-style
        "assistant that can see the app" for apps winSpark can't automate."""
        from winspark.connectors import openai_client, window_ocr

        api_key, model, base_url = self._read_openai_config()
        if not api_key:
            return False, (
                "No AI key set — open WhatsApp on the left, pick the AI reply method, "
                "and save your OpenAI or Groq key with Test connection."
            )

        capture = window_ocr.read_window_text(window_handle)
        if not capture.ok:
            return False, capture.error

        system = (
            "You are a helpful assistant looking at the user's screen. The text below was "
            "read (via OCR) from one application window on their Windows PC, so it may have "
            "minor recognition errors and lost layout. Answer the user's question about this "
            "app concisely and plainly.\n\n--- Screen text ---\n" + capture.text
        )
        try:
            reply = self._submit(
                openai_client.generate_reply_async(api_key, model, system, question, base_url=base_url)
            )
        except Exception as ex:  # noqa: BLE001
            return False, str(ex)
        return (reply.ok, reply.text if reply.ok else reply.error)

    def get_recent_messages(self, limit: int = 15):
        """(active_conversation_name, [WhatsAppMessage]) for the chat currently
        open in WhatsApp. A cheap accessibility-tree read — it does NOT open or
        foreground anything, so it's safe to poll on a timer. Returns (None, [])
        when WhatsApp isn't available or no conversation is open."""
        if self._connector is None:
            return None, []
        try:
            handle = self._submit(self._connector.find_window_async())
            if handle is None:
                return None, []
            active, messages = self._submit(self._connector.read_open_conversation_async(handle, limit))
            return active, list(messages)
        except Exception:  # noqa: BLE001
            logger.warning("get_recent_messages failed", exc_info=True)
            return None, []

    # --- guided flow helpers (used by the WhatsApp panel) ---------------

    def can_find_chat(self, chat_name: str) -> bool:
        """Whether the named chat can be found — in the visible recents list or,
        failing that, via WhatsApp's search box (same resolution path used when
        actually sending). Falls back to a recents-only check when the Windows
        sender isn't available (e.g. off-Windows)."""
        from winspark.connectors.whatsapp_chat_name_rules import chat_names_match

        if self._group_sender is not None:
            try:
                return self._submit(self._group_sender.can_resolve_chat_async(chat_name), timeout=60)
            except Exception:  # noqa: BLE001
                logger.warning("can_find_chat via resolver failed; falling back to recents", exc_info=True)

        chats = self.get_whatsapp_chats() or []
        target = chat_name.strip().lower()
        return any(c.chat_name.strip().lower() == target or chat_names_match(chat_name, c.chat_name) for c in chats)

    # --- app-wide AI configuration (OpenAI / Groq) ---------------------

    def get_ai_provider(self) -> str:
        return (self._settings.get_value(SETTINGS_AI_PROVIDER) or "").strip().lower() or DEFAULT_AI_PROVIDER

    def get_openai_api_key(self) -> str:
        return self._settings.get_value(SETTINGS_OPENAI_API_KEY) or ""

    def get_openai_model(self) -> str:
        saved = (self._settings.get_value(SETTINGS_OPENAI_MODEL) or "").strip()
        return saved or ai_provider_info(self.get_ai_provider())["default_model"]

    def set_openai_config(self, api_key: str, model: str = "", provider: str = "") -> None:
        if provider:
            self._settings.set_value(SETTINGS_AI_PROVIDER, provider.strip().lower())
        self._settings.set_value(SETTINGS_OPENAI_API_KEY, (api_key or "").strip())
        model = (model or "").strip() or ai_provider_info(self.get_ai_provider())["default_model"]
        self._settings.set_value(SETTINGS_OPENAI_MODEL, model)

    def _read_openai_config(self) -> tuple[str, str, str]:
        """Provider handed to the relay so AI-backed bindings get the current
        app-wide key/model/base-url at poll time (not whatever was set at
        construction)."""
        base_url = ai_provider_info(self.get_ai_provider())["base_url"]
        return self.get_openai_api_key(), self.get_openai_model(), base_url

    def test_openai_connection(self) -> tuple[bool, str]:
        """Check the saved AI key/model against the selected provider. Returns
        (ok, plain-English detail)."""
        from winspark.connectors import openai_client

        base_url = ai_provider_info(self.get_ai_provider())["base_url"]
        try:
            result = self._submit(
                openai_client.probe_async(self.get_openai_api_key(), self.get_openai_model(), base_url=base_url)
            )
        except Exception as ex:  # noqa: BLE001
            return False, str(ex)
        return result.ok, (result.text if result.ok else result.error)

    def test_message_source(self, url: str, chat: str) -> tuple[bool, str]:
        """Try to reach the message source. Returns (ok, plain-English detail)."""
        from winspark.connectors import fetch_webhook_client
        from winspark.connectors.fetch_webhook_url import try_validate_poll_url
        from winspark.ui.activity import friendly_reason

        resolved = normalize_poll_url(url, chat)
        ok, err = try_validate_poll_url(resolved)
        if not ok:
            return False, friendly_reason(err) or "That address doesn't look right."

        if _is_localhost(resolved):
            # The built-in test source only answers while its server is running.
            self._mock_server.ensure_started(FetchWebhookDefaults.MOCK_PORT)

        try:
            result = self._submit(fetch_webhook_client.probe_async(resolved, ""))
        except Exception as ex:  # noqa: BLE001
            return False, friendly_reason(str(ex))
        return (result.ok, "Connected" if result.ok else (friendly_reason(result.message) or "Couldn't connect."))

    def get_chat_binding(self, chat: str) -> Optional[WhatsAppFetchBindingEntity]:
        target = chat.strip().lower()
        return next((b for b in self._repository.get_bindings() if b.group_name.strip().lower() == target), None)

    def is_chat_automation_running(self, chat: str) -> bool:
        binding = self.get_chat_binding(chat)
        return self.is_relay_enabled() and binding is not None and binding.is_enabled

    def start_chat_automation(
        self,
        chat: str,
        url: str,
        interval: int,
        reply_source: str = "web",
        ai_mode: str = "reply",
        ai_prompt: str = "",
        trigger_text: str = "",
        reply_text: str = "",
    ) -> None:
        self.add_or_update_binding(
            chat, url, interval, enabled=True, reply_source=reply_source, ai_mode=ai_mode,
            ai_prompt=ai_prompt, trigger_text=trigger_text, reply_text=reply_text,
        )
        if not self.is_relay_enabled():
            self.set_relay_enabled(True)

    def stop_chat_automation(self, chat: str) -> None:
        binding = self.get_chat_binding(chat)
        if binding is not None:
            self.set_binding_enabled(binding.binding_id, False)

    def send_test_to_source(self, chat: str, text: str) -> None:
        """Queue a message into the built-in test source for a chat (so Start
        can relay it) — the friendly wrapper over inject_test_message."""
        self.inject_test_message(chat, text)


class _NoopGroupSender:
    """Stand-in sender used off-Windows so the UI still manages bindings and
    shows history; every send reports a clear failure rather than pretending."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def send_to_group_async(self, group_name: str, message_text: str):
        from winspark.connectors.fetch_webhook_models import WhatsAppGroupSendResult

        return WhatsAppGroupSendResult.failed(f"Sending unavailable: {self._reason}")
