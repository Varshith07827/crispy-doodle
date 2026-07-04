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
    DEFAULT_AGENT_MODE,
    DEFAULT_AI_PROVIDER,
    SETTINGS_AGENT_MODE,
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
    AutomationRuleRepository,
    EventRepository,
    LogRepository,
    SettingsRepository,
)
from winspark.domain.entities import AutomationRuleEntity, EventEntity
from winspark.domain.models import WindowInfo
from winspark.eventbus.bus import EventBus
from winspark.ui.activity import describe_activity
from winspark.ui.apps import RunningApp, detect_running_apps

logger = logging.getLogger(__name__)

# --- saved automations ------------------------------------------------------
# A person-facing "automation" is a named, saved action they can run on demand.
# It's stored in the generic AutomationRules table with a "manual" trigger; the
# action details live in ActionsJson so new kinds don't need schema changes.
# Two kinds today: send a WhatsApp message, or do something in an app via the
# agent (which covers "search Chrome for…" and any other free-text instruction).

import json as _json  # noqa: E402 - kept local to the automations helpers
from dataclasses import dataclass  # noqa: E402

AUTOMATION_WHATSAPP = "whatsapp_message"
AUTOMATION_APP_ACTION = "app_action"
_MANUAL_TRIGGER = "manual"


@dataclass(frozen=True, slots=True)
class Automation:
    """One saved, runnable action, as the UI sees it."""

    id: Optional[int]
    name: str
    kind: str            # AUTOMATION_WHATSAPP | AUTOMATION_APP_ACTION
    target: str          # chat name, or the target app's process name
    target_display: str  # human name of the target (chat name, or "Chrome")
    instruction: str     # the message to send, or the goal for the agent
    enabled: bool = True

    def summary(self) -> str:
        """One plain-English line describing what this automation does."""
        if self.kind == AUTOMATION_WHATSAPP:
            return f'Message “{self.target_display}”: {self.instruction}'.strip()
        where = self.target_display or self.target or "an app"
        return f'In {where}: {self.instruction}'.strip()


def _automation_to_rule(a: Automation) -> AutomationRuleEntity:
    actions = _json.dumps([{
        "kind": a.kind,
        "target": a.target,
        "target_display": a.target_display,
        "instruction": a.instruction,
    }])
    return AutomationRuleEntity(
        id=a.id,
        name=a.name,
        description=a.summary(),
        is_enabled=a.enabled,
        trigger_type_id=_MANUAL_TRIGGER,
        actions_json=actions,
    )


def _rule_to_automation(rule: AutomationRuleEntity) -> Automation:
    kind, target, target_display, instruction = AUTOMATION_APP_ACTION, "", "", ""
    try:
        actions = _json.loads(rule.actions_json or "[]")
        if actions:
            first = actions[0]
            kind = first.get("kind", kind)
            target = first.get("target", "")
            target_display = first.get("target_display", "")
            instruction = first.get("instruction", "")
    except (ValueError, TypeError, AttributeError):
        pass  # a malformed row degrades to an empty app-action, still listable
    return Automation(
        id=rule.id,
        name=rule.name,
        kind=kind,
        target=target,
        target_display=target_display,
        instruction=instruction,
        enabled=rule.is_enabled,
    )

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

        # Generic screen watchers: OCR any app's window on a timer and act when
        # the watched text appears. Own scheduler so watcher ticks never queue
        # behind WhatsApp relay polls (and vice versa).
        from winspark.connectors.screen_watch import ScreenWatcherRepository, ScreenWatchService

        self._watch_repository = ScreenWatcherRepository(connection_factory)
        self._automation_repository = AutomationRuleRepository(connection_factory)
        self._watch_scheduler = FetchWebhookBindingScheduler()
        self._watch_service = ScreenWatchService(
            self._watch_repository,
            self._watch_scheduler,
            find_window=self._find_window_for_watcher,
            read_screen=self._read_screen_for_watcher,
            ai_config_provider=self._read_openai_config,
            send_whatsapp=self._send_whatsapp_for_watcher,
        )
        self._notifications: deque = deque(maxlen=50)
        self._watch_service.on_notification(lambda title, body: self._notifications.append((title, body)))
        self._watch_service.on_activity(self._on_activity)

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
        #
        # Fire-and-forget (no .result()): enabling the relay immediately polls
        # every enabled automation — with several saved, and trigger/AI ones
        # driving WhatsApp, that blocked here for many seconds and the window
        # didn't appear until it finished. The engine loop resumes everything
        # in the background instead; the UI opens instantly.
        if self._read_relay_flag():
            asyncio.run_coroutine_threadsafe(self._resume_quietly(self._relay_service.set_relay_enabled_async(True), "automation"), self._loop)

        # Screen watchers resume on their own — they're read-only (OCR of a
        # background window; no clicking, typing, or foregrounding), so picking
        # them back up automatically can't act on anything by surprise.
        asyncio.run_coroutine_threadsafe(self._resume_quietly(self._watch_service.start_async(), "screen watchers"), self._loop)

    @staticmethod
    async def _resume_quietly(coro, what: str) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            logger.warning("Failed to resume %s from saved state", what, exc_info=True)

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
        self._watch_scheduler.dispose()
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

    # --- screen watchers (watch any app, act when text appears) ---------

    def get_watchers(self):
        return self._watch_repository.get_watchers()

    def add_watcher(
        self,
        process_name: str,
        title_hint: str,
        display_name: str,
        watch_text: str,
        action_kind: str = "notify",
        whatsapp_chat: str = "",
        whatsapp_message: str = "",
        interval: int = 10,
    ) -> None:
        from winspark.connectors.screen_watch import ScreenWatcherEntity

        watcher = ScreenWatcherEntity(
            process_name=process_name,
            window_title_hint=title_hint,
            app_display_name=display_name,
            watch_text=watch_text.strip(),
            action_kind=action_kind,
            whatsapp_chat=whatsapp_chat.strip(),
            whatsapp_message=whatsapp_message.strip(),
            poll_interval_seconds=max(5, interval),
        )
        self._submit(self._watch_service.add_watcher_async(watcher))

    def set_watcher_enabled(self, watcher_id: str, enabled: bool) -> None:
        self._submit(self._watch_service.set_enabled_async(watcher_id, enabled))

    def delete_watcher(self, watcher_id: str) -> None:
        self._submit(self._watch_service.delete_watcher_async(watcher_id))

    def pop_notifications(self) -> list[tuple[str, str]]:
        """Drain pending desktop notifications (title, body) for the UI to show."""
        items: list[tuple[str, str]] = []
        while self._notifications:
            try:
                items.append(self._notifications.popleft())
            except IndexError:  # pragma: no cover - race with producer
                break
        return items

    def _find_window_for_watcher(self, process_name: str, title_hint: str) -> Optional[int]:
        """Locate the watched app's window in the discovery snapshot. Prefers a
        title containing the hint, falls back to any window of the process."""
        wanted = process_name.strip().lower()
        hint = title_hint.strip().lower()
        candidates = [w for w in self.get_windows() if w.process_name.lower() == wanted]
        if not candidates:
            return None
        if hint:
            for window in candidates:
                if hint in window.title.lower():
                    return window.handle
        return candidates[0].handle

    def _read_screen_for_watcher(self, window_handle: int) -> tuple[bool, str]:
        from winspark.connectors import window_ocr

        result = window_ocr.read_window_text(window_handle)
        return (result.ok, result.text if result.ok else result.error)

    def capture_screen_image(self, window_handle: int) -> Optional[bytes]:
        """PNG bytes of the window (what OCR is looking at), or None."""
        from winspark.connectors import window_ocr

        return window_ocr.capture_window_png(window_handle)

    # --- the "Do it" agent (act on any app) -----------------------------

    def get_agent_mode(self) -> str:
        value = (self._settings.get_value(SETTINGS_AGENT_MODE) or "").strip()
        return value if value in ("ask_risky", "auto") else DEFAULT_AGENT_MODE

    def set_agent_mode(self, mode: str) -> None:
        if mode in ("ask_risky", "auto"):
            self._settings.set_value(SETTINGS_AGENT_MODE, mode)

    def agent_next_step(self, window_handle: int, app_name: str, goal: str, history: list[str]):
        """One turn of the closed-loop agent: look at the app AS IT IS NOW
        (controls + OCR), tell the AI what's been done so far, and get back
        either "done" or the single next step. Returns (ok, StepDecision |
        plain-English error). Heavy: call from a worker thread."""
        import hashlib
        from dataclasses import replace as dc_replace

        from winspark.automation import screen_agent
        from winspark.connectors import openai_client

        api_key, model, base_url = self._read_openai_config()
        if not api_key:
            return False, "No AI key set — open WhatsApp on the left, pick the AI reply method, and save your key."
        if self._sta_manager is None:
            return False, "Doing things in apps is only available on Windows."

        try:
            controls = self._submit(
                self._sta_manager.invoke_async(lambda: screen_agent.list_controls_sync(window_handle))
            )
        except Exception:  # noqa: BLE001
            logger.warning("control enumeration failed", exc_info=True)
            return False, "Couldn't read this app's buttons and fields."
        if not controls:
            return False, "This app doesn't expose anything winSpark can act on."

        _ocr_ok, screen_text = self._read_screen_for_watcher(window_handle)
        if not _ocr_ok:
            screen_text = ""
        digest = hashlib.sha256(screen_text.encode("utf-8")).hexdigest()

        prompt = screen_agent.build_step_user_prompt(app_name, goal, controls, screen_text, history)
        # Small models occasionally emit malformed JSON; the parser fails
        # closed, and one retry smooths over the flakiness without looping.
        error = ""
        for _attempt in range(2):
            try:
                reply = self._submit(
                    openai_client.complete_json_async(api_key, model, screen_agent.STEP_SYSTEM_PROMPT, prompt, base_url=base_url)
                )
            except Exception as ex:  # noqa: BLE001
                return False, str(ex)
            if not reply.ok:
                return False, reply.error
            decision, error = screen_agent.parse_step(reply.text, controls, goal)
            if decision is not None:
                return True, dc_replace(decision, screen_digest=digest)
        return False, error

    def agent_execute_step(self, window_handle: int, step) -> tuple[bool, str]:
        """Run one validated step on the STA thread. Heavy + drives a real
        app: call from a worker thread."""
        from winspark.automation import screen_agent

        if self._sta_manager is None:
            return False, "Doing things in apps is only available on Windows."
        results = self._submit(
            self._sta_manager.invoke_async(lambda: screen_agent.execute_plan_sync(window_handle, [step])),
            timeout=120,
        )
        return results[0] if results else (False, "Nothing happened.")

    def record_agent_result(self, summary: str) -> None:
        self._on_activity("", "agent_run", summary)

    # --- saved automations (create / manage / run on demand) ------------

    def get_automations(self) -> list[Automation]:
        """Every saved automation, newest first."""
        rules = self._automation_repository.get_all()
        automations = [_rule_to_automation(r) for r in rules]
        automations.sort(key=lambda a: a.id or 0, reverse=True)
        return automations

    def save_automation(
        self,
        automation_id: Optional[int],
        name: str,
        kind: str,
        target: str,
        target_display: str,
        instruction: str,
    ) -> int:
        """Create a new automation, or update the one with automation_id.
        Returns its id."""
        automation = Automation(
            id=automation_id,
            name=name.strip() or "Untitled automation",
            kind=kind,
            target=target.strip(),
            target_display=target_display.strip(),
            instruction=instruction.strip(),
            enabled=True,
        )
        rule = _automation_to_rule(automation)
        if automation_id is None:
            return self._automation_repository.insert(rule)
        rule.updated_at_utc = datetime.now(timezone.utc)
        self._automation_repository.update(rule)
        return automation_id

    def set_automation_enabled(self, automation_id: int, enabled: bool) -> None:
        self._automation_repository.set_enabled(automation_id, enabled)

    def delete_automation(self, automation_id: int) -> None:
        self._automation_repository.delete(automation_id)

    def run_automation(self, automation_id: int) -> tuple[bool, str]:
        """Run a saved automation right now. Drives real apps — call from a
        worker thread. Returns (success, plain-English result)."""
        rule = self._automation_repository.get_by_id(automation_id)
        if rule is None:
            return False, "That automation no longer exists."
        automation = _rule_to_automation(rule)
        if automation.kind == AUTOMATION_WHATSAPP:
            if not automation.target or not automation.instruction:
                return False, "This automation needs a chat and a message — edit it first."
            ok, detail = self.send_to_chat(automation.target, automation.instruction)
            self._on_activity("", "agent_run",
                              f"Ran “{automation.name}” — {'sent' if ok else 'failed: ' + detail}")
            return ok, ("Message sent." if ok else detail)
        return self._run_app_action(automation)

    def _run_app_action(self, automation: Automation) -> tuple[bool, str]:
        if not automation.instruction:
            return False, "This automation has no instruction — edit it first."
        handle, display = self._resolve_app_window(automation.target)
        if handle is None:
            where = automation.target_display or automation.target or "that app"
            return False, f"{where} isn't open right now — open it and run this again."
        ok, summary = self._run_agent_goal(handle, display or automation.target_display, automation.instruction)
        self._on_activity("", "agent_run",
                          f"Ran “{automation.name}” — {summary if ok else 'stopped: ' + summary}")
        return ok, summary

    def _resolve_app_window(self, process_name: str) -> tuple[Optional[int], str]:
        """Find a live window for the automation's target app by process name."""
        target = (process_name or "").lower()
        for app in self.get_running_apps():
            if app.process_name.lower() == target and app.window_handles:
                return app.window_handles[0], app.display_name
        return None, ""

    def _run_agent_goal(self, window_handle: int, app_name: str, goal: str) -> tuple[bool, str]:
        """Run the closed-loop agent to completion for a saved automation:
        look → decide → act, re-reading the app between steps. Unattended, so a
        risky step stops the run in "ask first" mode (there's no one to ask);
        in "just do it" mode it proceeds."""
        from winspark.automation.screen_agent import describe_step

        ask_first = self.get_agent_mode() == DEFAULT_AGENT_MODE  # "ask_risky"
        history: list[str] = []
        last_desc = last_digest = None
        for _round in range(8):
            ok, decision = self.agent_next_step(window_handle, app_name, goal, list(history))
            if not ok:
                return False, str(decision)
            if decision.done:
                return True, decision.summary or "Done."
            step = decision.step
            desc = describe_step(step)
            if desc == last_desc and decision.screen_digest and decision.screen_digest == last_digest:
                return False, "the app didn't respond to that step."
            last_desc, last_digest = desc, decision.screen_digest
            if step.risky and ask_first:
                return False, ("this needs a risky step — run it from the app's “Do it” box so you can "
                               "approve it, or switch the agent to “Just do it”.")
            step_ok, message = self.agent_execute_step(window_handle, step)
            history.append(f"{desc} -> {'ok' if step_ok else 'FAILED: ' + message}")
            if not step_ok:
                return False, message
        return False, "stopped after 8 steps — try a simpler instruction."

    async def _send_whatsapp_for_watcher(self, chat: str, text: str) -> tuple[bool, str]:
        if self._group_sender is None:
            return False, "Sending isn't available on this device."
        result = await self._group_sender.send_to_group_async(chat, text)
        return (result.success, result.status if result.success else result.failure_reason)

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
