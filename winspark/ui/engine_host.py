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
    AI_STYLES,
    DEFAULT_AGENT_MODE,
    DEFAULT_AI_PROVIDER,
    DEFAULT_AI_STYLE,
    DEFAULT_AI_WEB_SEARCH,
    DEFAULT_CHAT_MEMORY_MONGO_DB,
    SETTINGS_AGENT_MODE,
    SETTINGS_AI_STYLE,
    SETTINGS_AI_WEB_SEARCH,
    SETTINGS_CHAT_MEMORY_MONGO_DB,
    SETTINGS_CHAT_MEMORY_MONGO_URI,
    DEFAULT_WEBHOOK_TESTING,
    SETTINGS_AUTOMATIONS_PAUSED,
    SETTINGS_AI_PROVIDER,
    SETTINGS_OPENAI_API_KEY,
    SETTINGS_OPENAI_MODEL,
    SETTINGS_WEBHOOK_TESTING,
    SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED,
    ai_provider_info,
)
from winspark.data.chat_memory import MongoChatMemoryStore, build_chat_memory_store, copy_chat_memory
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
# A person-facing "automation" is a named, saved action plus a trigger that says
# WHEN to run it. It's stored in the generic AutomationRules table: the action
# lives in ActionsJson (so new kinds need no schema change), the trigger in
# TriggerTypeId + TriggerConfigJson (already in the schema). Actions: send a
# WhatsApp message, or do something in an app via the agent. Triggers: run it
# yourself (manual), on a schedule (interval / daily time), or when a watched
# app's screen shows some text.

import json as _json  # noqa: E402 - kept local to the automations helpers
from dataclasses import dataclass  # noqa: E402

AUTOMATION_WHATSAPP = "whatsapp_message"
AUTOMATION_APP_ACTION = "app_action"

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULE = "schedule"
TRIGGER_SCREEN = "screen"

# How often the trigger runner checks whether any automation is due to fire.
_AUTOMATION_TICK_SECONDS = 15


@dataclass(frozen=True, slots=True)
class Automation:
    """One saved, runnable action plus its trigger, as the UI sees it."""

    id: Optional[int]
    name: str
    kind: str            # AUTOMATION_WHATSAPP | AUTOMATION_APP_ACTION
    target: str          # chat name, or the target app's process name
    target_display: str  # human name of the target (chat name, or "Chrome")
    instruction: str     # the message to send, or the goal for the agent
    enabled: bool = True
    # trigger — how this automation gets run
    trigger_type: str = TRIGGER_MANUAL        # manual | schedule | screen
    schedule_mode: str = "interval"           # interval | daily
    interval_minutes: int = 60
    daily_time: str = "09:00"                 # HH:MM, 24h, for daily schedules
    watch_process: str = ""                   # for screen triggers: app to watch
    watch_display: str = ""
    watch_text: str = ""                      # fire when this appears on that app
    watch_mode: str = "literal"               # literal | meaning (AI-semantic)

    def summary(self) -> str:
        """One plain-English line describing what this automation does."""
        if self.kind == AUTOMATION_WHATSAPP:
            return f'Message “{self.target_display}”: {self.instruction}'.strip()
        where = self.target_display or self.target or "an app"
        return f'In {where}: {self.instruction}'.strip()

    def trigger_summary(self) -> str:
        """Plain-English 'when it runs', for the list."""
        if self.trigger_type == TRIGGER_SCHEDULE:
            if self.schedule_mode == "daily":
                return f"Every day at {self.daily_time}"
            return f"Every {_minutes_phrase(self.interval_minutes)}"
        if self.trigger_type == TRIGGER_SCREEN:
            where = self.watch_display or self.watch_process or "an app"
            by_meaning = " (by meaning)" if self.watch_mode == "meaning" else ""
            return f'When {where} shows “{self.watch_text}”{by_meaning}'
        return "When you run it"


def _minutes_phrase(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return "hour" if hours == 1 else f"{hours} hours"
    return "minute" if minutes == 1 else f"{minutes} minutes"


def _automation_to_rule(a: Automation) -> AutomationRuleEntity:
    actions = _json.dumps([{
        "kind": a.kind,
        "target": a.target,
        "target_display": a.target_display,
        "instruction": a.instruction,
    }])
    trigger_config = _json.dumps({
        "schedule_mode": a.schedule_mode,
        "interval_minutes": a.interval_minutes,
        "daily_time": a.daily_time,
        "watch_process": a.watch_process,
        "watch_display": a.watch_display,
        "watch_text": a.watch_text,
        "watch_mode": a.watch_mode,
    })
    return AutomationRuleEntity(
        id=a.id,
        name=a.name,
        description=a.summary(),
        is_enabled=a.enabled,
        trigger_type_id=a.trigger_type or TRIGGER_MANUAL,
        trigger_config_json=trigger_config,
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
    cfg = {}
    try:
        cfg = _json.loads(rule.trigger_config_json or "{}")
    except (ValueError, TypeError):
        pass
    trigger_type = rule.trigger_type_id or TRIGGER_MANUAL
    if trigger_type not in (TRIGGER_MANUAL, TRIGGER_SCHEDULE, TRIGGER_SCREEN):
        trigger_type = TRIGGER_MANUAL  # tolerate legacy/unknown trigger ids
    return Automation(
        id=rule.id,
        name=rule.name,
        kind=kind,
        target=target,
        target_display=target_display,
        instruction=instruction,
        enabled=rule.is_enabled,
        trigger_type=trigger_type,
        schedule_mode=cfg.get("schedule_mode", "interval"),
        interval_minutes=int(cfg.get("interval_minutes", 60) or 60),
        daily_time=cfg.get("daily_time", "09:00"),
        watch_process=cfg.get("watch_process", ""),
        watch_display=cfg.get("watch_display", ""),
        watch_text=cfg.get("watch_text", ""),
        watch_mode=cfg.get("watch_mode", "literal"),
    )


def _schedule_is_due(automation: "Automation", now: datetime, last_fire: Optional[datetime]) -> bool:
    """Pure decision: should a scheduled automation fire at `now`, given when it
    last fired (None = never this session)? Interval fires every N minutes since
    the last fire; daily fires once when the clock reaches HH:MM each day. Both
    are seeded with last_fire=now on load so nothing fires the instant the app
    starts."""
    if automation.schedule_mode == "daily":
        try:
            hh, mm = (int(p) for p in automation.daily_time.split(":", 1))
        except (ValueError, AttributeError):
            return False
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < target:
            return False
        return last_fire is None or last_fire < target
    minutes = max(1, automation.interval_minutes)
    if last_fire is None:
        return False  # seeded on load; first fire is one interval later
    return (now - last_fire).total_seconds() >= minutes * 60

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

        # Per-chat memory store: MongoDB when configured + reachable, else the
        # SQLite repository. Chosen once at startup; falls back silently.
        self._chat_memory = build_chat_memory_store(
            self._settings.get_value(SETTINGS_CHAT_MEMORY_MONGO_URI) or "",
            self._repository,
            database=self._settings.get_value(SETTINGS_CHAT_MEMORY_MONGO_DB) or DEFAULT_CHAT_MEMORY_MONGO_DB,
        )

        group_sender = self._build_group_sender()
        self._relay_service = WhatsAppFetchRelayService(
            self._repository,
            LogRepository(connection_factory),
            group_sender,
            self._mock_server,
            self._scheduler,
            openai_config_provider=self._read_reply_config,
            chat_memory=self._chat_memory,
        )
        # Make the built-in inbox link LIVE: a POST to it forwards to the chat
        # immediately, not on the next poll tick.
        self._inbox_bind_lock = threading.Lock()
        self._mock_server.on_message_injected(self._on_inbox_message)

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
        # Trigger runner state (in-memory; recomputed on restart). last_fire per
        # scheduled automation, last screen-match state per screen automation
        # (for edge-triggering), and which automations are mid-run (so a slow
        # run can't be started twice).
        self._automation_last_fire: dict[int, datetime] = {}
        self._automation_matched: dict[int, bool] = {}
        self._automation_screen_hash: dict[int, str] = {}
        self._automation_running: set[int] = set()
        self._automation_task = None
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

        # If automations were lost (empty tables) but a backup exists, bring
        # them back before anything resumes, so they get scheduled normally.
        self._maybe_restore_automations()

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

        # Trigger runner: fires automations on a schedule or when a watched
        # screen shows text. Lives on the engine loop; seeds schedules first so
        # nothing fires the instant the app opens.
        def _start_runner() -> None:
            self._automation_task = self._loop.create_task(self._automation_runner_loop())

        self._loop.call_soon_threadsafe(_start_runner)

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
        if self._automation_task is not None and self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._automation_task.cancel)
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

    def get_ai_style(self) -> str:
        value = (self._settings.get_value(SETTINGS_AI_STYLE) or "").strip().lower()
        return value if value in AI_STYLES else DEFAULT_AI_STYLE

    def set_ai_style(self, style: str) -> None:
        if style in AI_STYLES:
            self._settings.set_value(SETTINGS_AI_STYLE, style)

    def _ai_temperature(self) -> float:
        return AI_STYLES.get(self.get_ai_style(), 0.0)

    # --- agent memory: what worked before, per app -----------------------

    def recall_agent_memory(self, app_name: str) -> list[str]:
        """Short 'what worked before' lines for this app, oldest first."""
        raw = self._settings.get_value(f"agent.memory.{app_name.strip().lower()}") or "[]"
        try:
            items = _json.loads(raw)
            return [str(i) for i in items if str(i).strip()][-10:]
        except (ValueError, TypeError):
            return []

    def remember_agent_success(self, app_name: str, goal: str, steps_taken: list[str]) -> None:
        """Store a compact recipe for a run that finished: the goal and the
        steps that got there. Fed back into future runs on the same app so the
        agent starts from experience instead of zero."""
        app_key = app_name.strip().lower()
        if not app_key or not goal.strip():
            return
        succeeded = [s.split(" -> ")[0] for s in steps_taken if s.endswith("-> ok")]
        recipe = f"Goal “{goal.strip()[:80]}”: " + ("; ".join(succeeded[-6:]) if succeeded else "finished directly")
        memory = self.recall_agent_memory(app_name)
        memory = [m for m in memory if not m.startswith(f"Goal “{goal.strip()[:80]}”")]
        memory.append(recipe[:400])
        self._settings.set_value(f"agent.memory.{app_key}", _json.dumps(memory[-10:]))

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

        prompt = screen_agent.build_step_user_prompt(
            app_name, goal, controls, screen_text, history,
            learned=self.recall_agent_memory(app_name),
        )
        # Small models occasionally emit malformed JSON; the parser fails
        # closed, and one retry smooths over the flakiness without looping.
        error = ""
        for _attempt in range(2):
            try:
                reply = self._submit(
                    openai_client.complete_json_async(
                        api_key, model, screen_agent.STEP_SYSTEM_PROMPT, prompt,
                        base_url=base_url, temperature=self._ai_temperature(),
                    )
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

    # --- automation backup: never lose what the user set up -------------
    # The DB is the source of truth, but a wiped/rolled-back table (bad exit,
    # disk hiccup, OneDrive-style interference) silently erased everything the
    # user had programmed. After every change we snapshot the automations to a
    # JSON file next to the DB; on startup, an EMPTY table + a non-empty backup
    # means the data was lost rather than deleted (deletes re-snapshot the
    # post-delete state), so we restore automatically and say so in Activity.

    def _backup_path(self):
        # Next to whatever DB this engine actually uses (tests run on temp
        # databases — their backups must never touch the real folder).
        return self._factory.database_path.parent / "automations-backup.json"

    def _backup_automations(self) -> None:
        try:
            bindings = [{
                "group_name": b.group_name, "fetch_url": b.fetch_url, "api_key": b.api_key,
                "poll_interval_seconds": b.poll_interval_seconds, "is_enabled": b.is_enabled,
                "reply_source": b.reply_source, "ai_mode": b.ai_mode, "ai_prompt": b.ai_prompt,
                "trigger_text": b.trigger_text, "reply_text": b.reply_text,
            } for b in self._repository.get_bindings()]
            rules = [{
                "name": a.name, "kind": a.kind, "target": a.target,
                "target_display": a.target_display, "instruction": a.instruction,
                "enabled": a.enabled, "trigger_type": a.trigger_type,
                "schedule_mode": a.schedule_mode, "interval_minutes": a.interval_minutes,
                "daily_time": a.daily_time, "watch_process": a.watch_process,
                "watch_display": a.watch_display, "watch_text": a.watch_text,
                "watch_mode": a.watch_mode,
            } for a in self.get_automations()]
            payload = _json.dumps({"bindings": bindings, "rules": rules}, indent=1)
            path = self._backup_path()
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)  # atomic: never a half-written backup
        except Exception:  # noqa: BLE001 - a failed backup must never break a save
            logger.warning("automations backup failed", exc_info=True)

    def _maybe_restore_automations(self) -> None:
        try:
            path = self._backup_path()
            if not path.exists():
                return
            data = _json.loads(path.read_text(encoding="utf-8"))
            restored = 0
            if not self._repository.get_bindings():
                for b in data.get("bindings", []):
                    entity = WhatsAppFetchBindingEntity(
                        group_name=b.get("group_name", ""), fetch_url=b.get("fetch_url", ""),
                        api_key=b.get("api_key", ""),
                        poll_interval_seconds=int(b.get("poll_interval_seconds", 60) or 60),
                        is_enabled=bool(b.get("is_enabled", False)),
                        reply_source=b.get("reply_source", "web"), ai_mode=b.get("ai_mode", "reply"),
                        ai_prompt=b.get("ai_prompt", ""), trigger_text=b.get("trigger_text", ""),
                        reply_text=b.get("reply_text", ""),
                    )
                    if entity.group_name:
                        self._repository.upsert_binding(entity)
                        restored += 1
            if not self._automation_repository.get_all():
                for r in data.get("rules", []):
                    if not str(r.get("name", "")).strip():
                        continue
                    self.save_automation(
                        None, r.get("name", ""), r.get("kind", AUTOMATION_WHATSAPP),
                        r.get("target", ""), r.get("target_display", ""), r.get("instruction", ""),
                        trigger_type=r.get("trigger_type", TRIGGER_MANUAL),
                        schedule_mode=r.get("schedule_mode", "interval"),
                        interval_minutes=int(r.get("interval_minutes", 60) or 60),
                        daily_time=r.get("daily_time", "09:00"),
                        watch_process=r.get("watch_process", ""),
                        watch_display=r.get("watch_display", ""),
                        watch_text=r.get("watch_text", ""),
                        watch_mode=r.get("watch_mode", "literal"),
                    )
                    restored += 1
            if restored:
                self._on_activity("", "agent_run", f"Restored {restored} automation(s) from backup")
                logger.info("Restored %d automation(s) from backup", restored)
        except Exception:  # noqa: BLE001 - a broken backup must never block startup
            logger.warning("automations restore failed", exc_info=True)

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
        trigger_type: str = TRIGGER_MANUAL,
        schedule_mode: str = "interval",
        interval_minutes: int = 60,
        daily_time: str = "09:00",
        watch_process: str = "",
        watch_display: str = "",
        watch_text: str = "",
        watch_mode: str = "literal",
    ) -> int:
        """Create a new automation, or update the one with automation_id.
        Returns its id."""
        # Editing preserves the enabled state; a brand-new automation starts on.
        enabled = True
        if automation_id is not None:
            existing = self._automation_repository.get_by_id(automation_id)
            if existing is not None:
                enabled = existing.is_enabled
        automation = Automation(
            id=automation_id,
            name=name.strip() or "Untitled automation",
            kind=kind,
            target=target.strip(),
            target_display=target_display.strip(),
            instruction=instruction.strip(),
            enabled=enabled,
            trigger_type=trigger_type,
            schedule_mode=schedule_mode,
            interval_minutes=interval_minutes,
            daily_time=daily_time,
            watch_process=watch_process.strip(),
            watch_display=watch_display.strip(),
            watch_text=watch_text.strip(),
            watch_mode=watch_mode,
        )
        rule = _automation_to_rule(automation)
        if automation_id is None:
            new_id = self._automation_repository.insert(rule)
            self._seed_automation_schedule(new_id)
            self._backup_automations()
            return new_id
        rule.updated_at_utc = datetime.now(timezone.utc)
        self._automation_repository.update(rule)
        self._seed_automation_schedule(automation_id)  # re-seed after edits
        self._backup_automations()
        return automation_id

    def set_automation_enabled(self, automation_id: int, enabled: bool) -> None:
        self._automation_repository.set_enabled(automation_id, enabled)
        self._backup_automations()

    def delete_automation(self, automation_id: int) -> None:
        self._automation_repository.delete(automation_id)
        self._backup_automations()

    def run_automation(self, automation_id: int, progress=None, ask_user=None) -> tuple[bool, str]:
        """Run a saved automation right now. Drives real apps — call from a
        worker thread. `progress` is an optional callback(str) that receives a
        line each time something happens (for a live step log). `ask_user` is
        an optional callback(question) -> answer-or-None: when given (a manual
        Run now, with the user watching), the agent's mid-run questions go to
        it instead of aborting the run. Returns (success, plain-English
        result)."""
        rule = self._automation_repository.get_by_id(automation_id)
        if rule is None:
            return False, "That automation no longer exists."
        automation = _rule_to_automation(rule)
        if automation.kind == AUTOMATION_WHATSAPP:
            if not automation.target or not automation.instruction:
                return False, "This automation needs a chat and a message — edit it first."
            if progress:
                progress(f"Sending to “{automation.target_display or automation.target}”…")
            ok, detail = self.send_to_chat(automation.target, automation.instruction)
            self._on_activity("", "agent_run",
                              f"Ran “{automation.name}” — {'sent' if ok else 'failed: ' + detail}")
            return ok, ("Message sent." if ok else detail)
        return self._run_app_action(automation, progress, ask_user)

    def _run_app_action(self, automation: Automation, progress=None, ask_user=None) -> tuple[bool, str]:
        if not automation.instruction:
            return False, "This automation has no instruction — edit it first."
        handle, display = self._resolve_app_window(automation.target)
        if handle is None:
            where = automation.target_display or automation.target or "that app"
            return False, f"{where} isn't open right now — open it and run this again."
        ok, summary = self._run_agent_goal(
            handle, display or automation.target_display, automation.instruction, progress, ask_user
        )
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

    def _run_agent_goal(self, window_handle: int, app_name: str, goal: str, progress=None,
                        ask_user=None) -> tuple[bool, str]:
        """Run the closed-loop agent to completion for a saved automation:
        look → decide → act, re-reading the app between steps. `progress`
        (optional) receives a line per step for a live log. `ask_user`
        (optional, callback(question) -> answer-or-None) makes the run
        ATTENDED: the agent's questions go to the user instead of aborting.
        Without it (scheduled/triggered runs) a question stops the run, and a
        risky step stops it in "ask first" mode — there's no one to ask."""
        from winspark.automation.screen_agent import describe_step

        ask_first = self.get_agent_mode() == DEFAULT_AGENT_MODE  # "ask_risky"
        history: list[str] = []
        last_desc = last_digest = None
        consecutive_failures = 0
        answered_questions: dict[str, str] = {}   # normalized question -> the user's answer
        question_repeats: dict[str, int] = {}
        # No one is watching an unattended run, so it can't ask questions or run
        # forever — a generous round cap replaces the old tight one; attended
        # runs (the Do-it box) have no cap and ask the user instead.
        for _round in range(25):
            ok, decision = self.agent_next_step(window_handle, app_name, goal, list(history))
            if not ok:
                return False, str(decision)
            if decision.done:
                self.remember_agent_success(app_name, goal, history)
                return True, decision.summary or "Done."
            if decision.question:
                if ask_user is None:
                    return False, f"needs your input — it asked: “{decision.question}” Run it from the app's “Do it” box to answer."
                # Never nag: an already-answered question gets the answer
                # replayed instead of re-prompting; a third repeat stops the run.
                key = decision.question.strip().lower()
                if key in answered_questions:
                    question_repeats[key] = question_repeats.get(key, 0) + 1
                    if question_repeats[key] >= 2:
                        return False, f"stopped — it kept re-asking “{decision.question}” despite your answer."
                    history.append(
                        f"Asked you: {decision.question} -> you said: {answered_questions[key]}"
                        " (already answered — do NOT ask this again)"
                    )
                    if progress:
                        progress("？ " + decision.question + "  (reusing your earlier answer)")
                    continue
                if progress:
                    progress("？ " + decision.question)
                answer = ask_user(decision.question)
                if answer is None:
                    return False, f"stopped at its question: “{decision.question}”"
                if progress:
                    progress("   ↳ you: " + answer)
                answered_questions[key] = answer
                history.append(f"Asked you: {decision.question} -> you said: {answer}")
                continue
            step = decision.step
            desc = describe_step(step)
            if desc == last_desc and decision.screen_digest and decision.screen_digest == last_digest:
                return False, "the app didn't respond to that step."
            last_desc, last_digest = desc, decision.screen_digest
            if progress:
                progress("→ " + desc + ("  ⚠" if step.risky else ""))
            if step.risky and ask_first:
                return False, ("this needs a risky step — run it from the app's “Do it” box so you can "
                               "approve it, or switch the agent to “Just do it”.")
            step_ok, message = self.agent_execute_step(window_handle, step)
            if progress:
                progress(("   ✓ " if step_ok else "   ✗ ") + message)
            history.append(f"{desc} -> {'ok' if step_ok else 'FAILED: ' + message}")
            if step_ok:
                consecutive_failures = 0
            else:
                # Fallback: tell the AI it failed and let it try a different
                # approach, rather than aborting on the first miss.
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    return False, f"kept failing — last problem: {message}"
        return False, "stopped after 25 steps — try a simpler instruction, or run it from the app's “Do it” box."

    # --- automation triggers (run on a schedule / when a screen shows text) --

    def _seed_automation_schedule(self, automation_id: int) -> None:
        """Mark a scheduled automation as 'last fired = now' so it doesn't fire
        the instant it's saved or the app starts — the first run is one interval
        (or the next daily time) later. Screen triggers reset their match state
        so they only fire on a fresh appearance."""
        self._automation_last_fire[automation_id] = datetime.now()
        self._automation_matched.pop(automation_id, None)
        self._automation_screen_hash.pop(automation_id, None)

    def _seed_all_schedules(self) -> None:
        for a in self.get_automations():
            if a.id is not None and a.trigger_type == TRIGGER_SCHEDULE:
                self._automation_last_fire.setdefault(a.id, datetime.now())

    def get_automations_paused(self) -> bool:
        """Master switch: when True the trigger runner fires nothing (manual
        'Run now' still works)."""
        return (self._settings.get_value(SETTINGS_AUTOMATIONS_PAUSED) or "").strip().lower() == "true"

    def set_automations_paused(self, paused: bool) -> None:
        self._settings.set_value(SETTINGS_AUTOMATIONS_PAUSED, "true" if paused else "false")

    def _automation_tick(self) -> None:
        """One pass of the trigger runner (called off the engine loop, in an
        executor thread, every few seconds). Fires any automation whose trigger
        is due; never blocks on the run itself (that happens on its own thread)."""
        if self.get_automations_paused():
            return
        now = datetime.now()
        for a in self.get_automations():
            if not a.enabled or a.id is None or a.id in self._automation_running:
                continue
            if a.trigger_type == TRIGGER_SCHEDULE:
                if _schedule_is_due(a, now, self._automation_last_fire.get(a.id)):
                    self._automation_last_fire[a.id] = now
                    self._fire_automation(a, a.trigger_summary())
            elif a.trigger_type == TRIGGER_SCREEN:
                verdict = self._screen_trigger_verdict(a)
                if verdict is None:
                    continue  # screen unchanged since last look — nothing to decide
                was_matched = self._automation_matched.get(a.id, False)
                self._automation_matched[a.id] = verdict
                if verdict and not was_matched:  # edge — only on fresh appearance
                    where = a.watch_display or a.watch_process
                    self._fire_automation(a, f'{where} showed “{a.watch_text}”')

    def _screen_trigger_verdict(self, automation: Automation) -> Optional[bool]:
        """Does the watched app currently show the trigger text? Returns True/
        False, or None when the screen hasn't changed since the last check (so
        the caller keeps the previous verdict and we skip re-running the match —
        this is what stops AI-semantic triggers from calling the model every
        tick)."""
        if not automation.watch_text.strip() or not automation.watch_process:
            return False
        handle = self._find_window_for_watcher(automation.watch_process, "")
        if handle is None:
            return False
        ok, text = self._read_screen_for_watcher(handle)
        if not ok or not text:
            return False
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self._automation_screen_hash.get(automation.id) == digest:
            return None
        self._automation_screen_hash[automation.id] = digest
        return self._screen_text_matches(automation, text)

    def _screen_text_matches(self, automation: Automation, screen_text: str) -> bool:
        """Literal substring first; for 'by meaning' triggers, fall back to the
        same AI intent-match the screen watchers use."""
        from winspark.connectors import trigger_match

        if trigger_match.literal_match(automation.watch_text, screen_text):
            return True
        if automation.watch_mode != "meaning":
            return False
        api_key, model, base_url = self._read_openai_config()
        if not api_key:
            return False
        from winspark.connectors import openai_client

        try:
            verdict = self._submit(
                openai_client.classify_intent_match_async(
                    api_key, model, automation.watch_text, screen_text[:4000], base_url=base_url
                )
            )
        except Exception:  # noqa: BLE001
            return False
        return bool(verdict)

    def _fire_automation(self, automation: Automation, reason: str) -> None:
        """Run a triggered automation on its own thread and toast the result."""
        automation_id = automation.id
        if automation_id is None or automation_id in self._automation_running:
            return
        self._automation_running.add(automation_id)
        self._on_activity("", "agent_run", f"“{automation.name}” triggered — {reason}")

        def worker() -> None:
            try:
                ok, message = self.run_automation(automation_id)
            except Exception as ex:  # noqa: BLE001
                ok, message = False, str(ex)
            finally:
                self._automation_running.discard(automation_id)
            title = "Automation ran" if ok else "Automation didn't finish"
            self._notifications.append((title, f"{automation.name}: {message}"))

        threading.Thread(target=worker, name=f"winSpark-automation-{automation_id}", daemon=True).start()

    async def _automation_runner_loop(self) -> None:
        """Periodic trigger check, living on the engine loop. Runs the (possibly
        blocking, OCR-doing) tick in an executor so the loop stays responsive."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._seed_all_schedules)
        while True:
            await asyncio.sleep(_AUTOMATION_TICK_SECONDS)
            try:
                await loop.run_in_executor(None, self._automation_tick)
            except Exception:  # noqa: BLE001
                logger.warning("automation trigger tick failed", exc_info=True)

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
            # A previous search (the find-a-chat fallback) can leave WhatsApp
            # showing filtered results — refreshing would then read that residue
            # instead of the recents list. Clearing first is a strict no-op
            # unless a search is actually active, so an open chat is never
            # disturbed.
            if self._sta_manager is not None:
                from winspark.connectors.whatsapp_group_sender import _clear_search_sync

                self._submit(self._sta_manager.invoke_async(lambda: _clear_search_sync(handle)))
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
        # One automation per TYPE per chat: matching (chat, source) updates in
        # place; a different source on the same chat is a second automation —
        # a web link, an AI reply, and a trigger can all run side by side.
        existing = next(
            (b for b in self._repository.get_bindings()
             if b.group_name.strip().lower() == group.lower() and b.reply_source == reply_source),
            None,
        )
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
        self._backup_automations()

    def set_binding_enabled(self, binding_id: str, enabled: bool) -> None:
        if enabled:
            self._submit(self._relay_service.resume_binding_async(binding_id))
        else:
            self._submit(self._relay_service.pause_binding_async(binding_id))
        self._backup_automations()

    def delete_binding(self, binding_id: str) -> None:
        self._submit(self._relay_service.delete_binding_async(binding_id))
        self._backup_automations()

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

    def list_browser_tabs(self, window_handle: int) -> list[tuple[str, bool]]:
        """Open browser tabs for a Chromium window as (title, is_current).
        Uncapped and excludes in-page tab strips (e.g. YouTube's category
        chips); [] off Windows or for a non-browser window."""
        if self._sta_manager is None:
            return []
        try:
            from winspark.automation import screen_agent

            return self._submit(
                self._sta_manager.invoke_async(lambda: screen_agent.list_browser_tabs_sync(window_handle))
            )
        except Exception:  # noqa: BLE001 - tabs are a bonus, never a blocker
            logger.warning("list_browser_tabs failed", exc_info=True)
            return []

    def activate_browser_tab(self, window_handle: int, tab_title: str) -> bool:
        """Switch a browser window to a specific open tab (by title). Returns
        whether it worked; [] / False off Windows or for a non-browser window."""
        if self._sta_manager is None:
            return False
        try:
            from winspark.automation import screen_agent

            return bool(self._submit(
                self._sta_manager.invoke_async(
                    lambda: screen_agent.activate_browser_tab_sync(window_handle, tab_title)
                )
            ))
        except Exception:  # noqa: BLE001
            logger.warning("activate_browser_tab failed", exc_info=True)
            return False

    def ask_about_screen(self, window_handle: int, question: str) -> tuple[bool, str]:
        """Answer a question about what's on an app's window: capture + OCR the
        window, then ask the configured AI service with the screen text as
        context. Returns (ok, answer-or-plain-error). This is the Comet-style
        "assistant that can see the app" for apps winSpark can't automate."""
        from winspark.connectors import openai_client, window_ocr

        api_key, model, base_url, fallback = self._read_reply_config()
        if not api_key:
            return False, (
                "No AI key set — open WhatsApp on the left, pick the AI reply method, "
                "and save your OpenAI or Groq key with Test connection."
            )

        capture = window_ocr.read_window_text(window_handle)
        if not capture.ok:
            return False, capture.error

        # OCR reads pixels, and some UI text is truncated ON SCREEN — browser
        # tab labels especially ("LeetCode - The World's Leadin"). The app's own
        # accessibility tree has the exact names, so hand ALL of them (uncapped,
        # in-page tab strips excluded) to the AI as ground truth alongside OCR.
        tabs_section = ""
        tabs = self.list_browser_tabs(window_handle)
        if tabs:
            lines = "\n".join(f"- {name}{'  (current)' if current else ''}" for name, current in tabs)
            tabs_section = (
                f"--- Open tabs ({len(tabs)}, exact names straight from the app — trust these over the OCR) ---\n"
                + lines + "\n\n"
            )

        system = (
            "You are a helpful assistant looking at the user's screen. The text below was "
            "read (via OCR) from one application window on their Windows PC, so it may have "
            "minor recognition errors and lost layout. Answer the user's question about this "
            "app concisely and plainly.\n\n" + tabs_section + "--- Screen text ---\n" + capture.text
        )
        try:
            reply = self._submit(
                openai_client.generate_reply_async(
                    api_key, model, system, question,
                    base_url=base_url, temperature=self._ai_temperature(),
                )
            )
            if not reply.ok and fallback and fallback != model:
                reply = self._submit(
                    openai_client.generate_reply_async(
                        api_key, fallback, system, question,
                        base_url=base_url, temperature=self._ai_temperature(),
                    )
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

    def get_openai_api_key(self, provider: str = "") -> str:
        """The saved key for a provider (default: the active one). Keys are
        stored per provider, so switching between OpenAI and Groq keeps each
        one's key — a Groq key isn't shown (or sent) while OpenAI is active."""
        provider = (provider or self.get_ai_provider()).strip().lower()
        scoped = self._settings.get_value(f"{SETTINGS_OPENAI_API_KEY}.{provider}")
        if scoped:
            return scoped
        # Migration: the pre-per-provider single key belonged to whatever
        # provider was active when it was saved.
        if provider == self.get_ai_provider():
            return self._settings.get_value(SETTINGS_OPENAI_API_KEY) or ""
        return ""

    def get_openai_model(self, provider: str = "") -> str:
        provider = (provider or self.get_ai_provider()).strip().lower()
        saved = (self._settings.get_value(f"{SETTINGS_OPENAI_MODEL}.{provider}") or "").strip()
        if not saved and provider == self.get_ai_provider():
            saved = (self._settings.get_value(SETTINGS_OPENAI_MODEL) or "").strip()
        return saved or ai_provider_info(provider)["default_model"]

    def set_openai_config(self, api_key: str, model: str = "", provider: str = "") -> None:
        provider = (provider or self.get_ai_provider()).strip().lower()
        self._settings.set_value(SETTINGS_AI_PROVIDER, provider)
        api_key = (api_key or "").strip()
        model = (model or "").strip() or ai_provider_info(provider)["default_model"]
        self._settings.set_value(f"{SETTINGS_OPENAI_API_KEY}.{provider}", api_key)
        self._settings.set_value(f"{SETTINGS_OPENAI_MODEL}.{provider}", model)
        # Keep the legacy single keys in sync with the active provider so any
        # older reader still works.
        self._settings.set_value(SETTINGS_OPENAI_API_KEY, api_key)
        self._settings.set_value(SETTINGS_OPENAI_MODEL, model)

    def webhook_pending_count(self) -> int:
        """How many messages posted to inbox links are queued but not yet sent
        (they drain as the relay sends them, one at a time)."""
        try:
            return self._mock_server.total_queued_count()
        except Exception:  # noqa: BLE001
            return 0

    def get_webhook_testing_enabled(self) -> bool:
        value = (self._settings.get_value(SETTINGS_WEBHOOK_TESTING) or "").strip().lower()
        if value in ("true", "false"):
            return value == "true"
        return DEFAULT_WEBHOOK_TESTING

    def set_webhook_testing_enabled(self, enabled: bool) -> None:
        self._settings.set_value(SETTINGS_WEBHOOK_TESTING, "true" if enabled else "false")

    def _on_inbox_message(self, group_name: str) -> None:
        """Fired on the mock server's HTTP thread when a POST queues a message.
        Deliver it to the chat NOW, through the relay so sends are SERIALIZED —
        the previous message finishes typing+sending before the next starts.
        (Firing concurrent sends raced on the one compose box: a burst of POSTs
        overwrote each other and only the last was sent.) If the chat has no
        automation yet, one is created on the spot. No-op when the automation
        master switch is off or webhook testing is disabled."""
        loop = self._loop
        if loop is None or not self._relay_service.is_relay_enabled or not self.get_webhook_testing_enabled():
            return
        target = (group_name or "").strip().lower()
        # One lock across the HTTP threads so a burst of POSTs to a new chat
        # creates exactly ONE binding, not one per request.
        with self._inbox_bind_lock:
            # A POST belongs to the chat's WEB automation specifically — the
            # chat may also run AI/trigger automations alongside it.
            binding = next(
                (b for b in self._repository.get_bindings()
                 if b.group_name.strip().lower() == target and b.reply_source == "web"),
                None,
            )
            newly_bound = binding is None
            if newly_bound:
                binding = WhatsAppFetchBindingEntity(
                    group_name=group_name.strip(),
                    fetch_url=normalize_poll_url("", group_name),
                    reply_source="web",
                    is_enabled=True,
                    poll_interval_seconds=FetchWebhookDefaults.MIN_POLL_INTERVAL_SECONDS,
                )
                self._repository.upsert_binding(binding)
        if not binding.is_enabled:
            return
        try:
            if newly_bound:
                asyncio.run_coroutine_threadsafe(self._relay_service.sync_scheduler_async(), loop)
                self._backup_automations()
                self._on_activity("", "agent_run", f"Started a webhook automation for {group_name.strip()}")
            # Serialized by the relay's cycle lock — each POST sends one message,
            # fully, before the next.
            asyncio.run_coroutine_threadsafe(
                self._relay_service.poll_binding_now_async(binding.binding_id), loop
            )
        except Exception:  # noqa: BLE001 - a delivery hiccup must not crash the HTTP thread
            logger.warning("immediate inbox delivery failed to schedule", exc_info=True)

    def _read_openai_config(self) -> tuple[str, str, str]:
        """Key/model/base-url for precise work (the acting agent, semantic
        matching) — always the user's configured model."""
        base_url = ai_provider_info(self.get_ai_provider())["base_url"]
        return self.get_openai_api_key(), self.get_openai_model(), base_url

    def get_ai_web_search(self) -> bool:
        value = (self._settings.get_value(SETTINGS_AI_WEB_SEARCH) or "").strip().lower()
        if value in ("true", "false"):
            return value == "true"
        return DEFAULT_AI_WEB_SEARCH

    def set_ai_web_search(self, enabled: bool) -> None:
        self._settings.set_value(SETTINGS_AI_WEB_SEARCH, "true" if enabled else "false")

    def _read_reply_config(self) -> tuple[str, str, str, str]:
        """Key/model/base-url/fallback-model for CONVERSATIONAL work (WhatsApp
        AI replies, screen questions). With web lookup on, the provider's
        web-search model answers — so "what's happening this week?" isn't stuck
        at a training cutoff — and the configured model is the fallback if the
        search model ever fails. The agent never uses this: acting in apps
        needs determinism, not browsing."""
        api_key, model, base_url = self._read_openai_config()
        if not self.get_ai_web_search():
            return api_key, model, base_url, ""
        search_model = ai_provider_info(self.get_ai_provider()).get("search_model", "")
        if not search_model:
            return api_key, model, base_url, ""
        return api_key, search_model, base_url, model

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

    def get_chat_binding(self, chat: str, reply_source: str = "") -> Optional[WhatsAppFetchBindingEntity]:
        """The chat's automation — of one type when `reply_source` is given,
        else whichever exists (a chat can hold one automation per type)."""
        target = chat.strip().lower()
        return next(
            (b for b in self._repository.get_bindings()
             if b.group_name.strip().lower() == target
             and (not reply_source or b.reply_source == reply_source)),
            None,
        )

    def get_chat_bindings(self, chat: str) -> list[WhatsAppFetchBindingEntity]:
        """All of a chat's automations (at most one per reply source)."""
        target = chat.strip().lower()
        return [b for b in self._repository.get_bindings() if b.group_name.strip().lower() == target]

    # --- chat memory (view / manage) -------------------------------------

    def chat_memory_backend(self) -> str:
        """"MongoDB" or "local storage" — what's actually storing chat memory
        right now (may be local even with a URI set, if Mongo was unreachable)."""
        return "MongoDB" if isinstance(self._chat_memory, MongoChatMemoryStore) else "local storage"

    def get_chat_memory(self, chat: str, limit: int = 200) -> list[tuple[str, str, str]]:
        """A chat's remembered messages, oldest first: (role, sender, text)."""
        try:
            return self._chat_memory.get_chat_memory(chat, limit)
        except Exception:  # noqa: BLE001 - a store hiccup must not break the UI
            logger.warning("reading chat memory failed", exc_info=True)
            return []

    def get_chats_with_memory(self) -> list[tuple[str, int]]:
        """Every chat that has remembered messages, with its count."""
        try:
            return self._chat_memory.get_chats_with_memory()
        except Exception:  # noqa: BLE001
            logger.warning("listing chat memory failed", exc_info=True)
            return []

    def clear_chat_memory(self, chat: str) -> None:
        try:
            self._chat_memory.clear_chat_memory(chat)
        except Exception:  # noqa: BLE001
            logger.warning("clearing chat memory failed", exc_info=True)

    def get_chat_memory_mongo_uri(self) -> str:
        return self._settings.get_value(SETTINGS_CHAT_MEMORY_MONGO_URI) or ""

    def set_chat_memory_mongo(self, uri: str, database: str = "") -> tuple[str, int]:
        """Save the MongoDB connection settings and rebuild the store now.
        Migrates existing local memory into MongoDB on first connect (chats
        already in Mongo are skipped, so it won't duplicate). Returns
        (backend-in-use, messages-migrated) so the UI can confirm whether the
        connection took ("MongoDB") or fell back ("local storage") and how much
        was carried over."""
        uri = (uri or "").strip()
        database = (database or "").strip() or DEFAULT_CHAT_MEMORY_MONGO_DB
        self._settings.set_value(SETTINGS_CHAT_MEMORY_MONGO_URI, uri)
        self._settings.set_value(SETTINGS_CHAT_MEMORY_MONGO_DB, database)
        old = self._chat_memory
        new_store = build_chat_memory_store(uri, self._repository, database=database)
        migrated = 0
        # Only migrate when we actually connected to Mongo and are leaving the
        # local SQLite store — copy what's in SQLite so nothing is left behind.
        if isinstance(new_store, MongoChatMemoryStore) and not isinstance(old, MongoChatMemoryStore):
            migrated = copy_chat_memory(self._repository, new_store)
        self._chat_memory = new_store
        self._relay_service.set_chat_memory(self._chat_memory)
        if isinstance(old, MongoChatMemoryStore) and old is not self._chat_memory:
            old.close()
        return self.chat_memory_backend(), migrated

    def is_chat_automation_running(self, chat: str, reply_source: str = "") -> bool:
        """Is an automation on for this chat — of one type when `reply_source`
        is given, else of any type."""
        if not self.is_relay_enabled():
            return False
        return any(
            b.is_enabled for b in self.get_chat_bindings(chat)
            if not reply_source or b.reply_source == reply_source
        )

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

    def stop_chat_automation(self, chat: str, reply_source: str = "") -> None:
        """Stop the chat's automation of one type — or all of them when no
        type is given."""
        for binding in self.get_chat_bindings(chat):
            if not reply_source or binding.reply_source == reply_source:
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
