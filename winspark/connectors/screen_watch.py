"""Generic screen watchers — keep an eye on ANY app's window and act when
something appears.

This is the first app-independent automation in winSpark: pick a running app,
say what to wait for in plain English ("Download complete", "Out for
delivery"), and choose what happens — a Windows notification, or a WhatsApp
message via the existing sender. The watcher OCRs the app's window on a timer
(PrintWindow works on background windows, so nothing is foregrounded or
clicked — watching is read-only and can't misfire into the wrong app).

Matching mirrors the WhatsApp trigger feature: literal word matching always
works; when an AI key is configured, the watch text is also matched by
meaning. The AI is only consulted when the screen's text actually changed
since the last check, so a 10-second poll doesn't bill per tick.

Watchers are one-shot: when the condition appears, the action fires once and
the watcher pauses itself (status "matched"). Resume it from the UI to arm it
again — that's the natural semantics for "tell me when X happens", and it can
never spam.

The .NET original approached this space with a hand-written agent per app
(NotepadAgent, BrowserAgent, SlackAgent, ...); this replaces all of that with
one OCR + LLM pipeline that works on any window.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from winspark.connectors import openai_client, trigger_match
from winspark.data.connection import ConnectionFactory

logger = logging.getLogger(__name__)

# How much screen text to hand the AI for semantic matching. OCR of a full
# window is typically under 2k chars; this cap just bounds the worst case.
_MAX_AI_TEXT_CHARS = 4000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ScreenWatcherEntity:
    """One watched app window + what to wait for + what to do."""

    watcher_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    process_name: str = ""
    window_title_hint: str = ""
    app_display_name: str = ""
    watch_text: str = ""
    action_kind: str = "notify"  # notify | whatsapp
    whatsapp_chat: str = ""
    whatsapp_message: str = ""
    poll_interval_seconds: int = 10
    is_enabled: bool = True
    status: str = ""  # "" | watching | app-not-open | matched | error
    last_error: str = ""
    matched_snippet: str = ""
    created_at_utc: datetime = field(default_factory=_utcnow)
    updated_at_utc: datetime = field(default_factory=_utcnow)

    # The shared FetchWebhookBindingScheduler drives anything exposing these
    # three names (binding_id / is_enabled / poll_interval_seconds, plus
    # group_name for its sort) — alias them so watchers can reuse it as-is.
    @property
    def binding_id(self) -> str:
        return self.watcher_id

    @property
    def group_name(self) -> str:
        return self.app_display_name or self.process_name


class ScreenWatcherRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def get_watchers(self) -> list[ScreenWatcherEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute("SELECT * FROM ScreenWatchers ORDER BY CreatedAtUtc").fetchall()
            return [_row_to_watcher(r) for r in rows]
        finally:
            conn.close()

    def get_watcher(self, watcher_id: str) -> Optional[ScreenWatcherEntity]:
        conn = self._factory.create_connection()
        try:
            row = conn.execute("SELECT * FROM ScreenWatchers WHERE WatcherId = ?", (watcher_id,)).fetchone()
            return _row_to_watcher(row) if row else None
        finally:
            conn.close()

    def upsert_watcher(self, watcher: ScreenWatcherEntity) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                """
                INSERT INTO ScreenWatchers
                    (WatcherId, ProcessName, WindowTitleHint, AppDisplayName, WatchText, ActionKind,
                     WhatsAppChat, WhatsAppMessage, PollIntervalSeconds, IsEnabled, Status, LastError,
                     MatchedSnippet, CreatedAtUtc, UpdatedAtUtc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(WatcherId) DO UPDATE SET
                    ProcessName = excluded.ProcessName,
                    WindowTitleHint = excluded.WindowTitleHint,
                    AppDisplayName = excluded.AppDisplayName,
                    WatchText = excluded.WatchText,
                    ActionKind = excluded.ActionKind,
                    WhatsAppChat = excluded.WhatsAppChat,
                    WhatsAppMessage = excluded.WhatsAppMessage,
                    PollIntervalSeconds = excluded.PollIntervalSeconds,
                    IsEnabled = excluded.IsEnabled,
                    UpdatedAtUtc = excluded.UpdatedAtUtc
                """,
                (
                    watcher.watcher_id,
                    watcher.process_name,
                    watcher.window_title_hint,
                    watcher.app_display_name,
                    watcher.watch_text,
                    watcher.action_kind,
                    watcher.whatsapp_chat,
                    watcher.whatsapp_message,
                    watcher.poll_interval_seconds,
                    int(watcher.is_enabled),
                    watcher.status,
                    watcher.last_error,
                    watcher.matched_snippet,
                    _iso(watcher.created_at_utc),
                    _iso(_utcnow()),
                ),
            )
        finally:
            conn.close()

    def delete_watcher(self, watcher_id: str) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute("DELETE FROM ScreenWatchers WHERE WatcherId = ?", (watcher_id,))
        finally:
            conn.close()

    def set_enabled(self, watcher_id: str, enabled: bool) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                "UPDATE ScreenWatchers SET IsEnabled = ?, Status = CASE WHEN ? THEN 'watching' ELSE Status END, UpdatedAtUtc = ? WHERE WatcherId = ?",
                (int(enabled), int(enabled), _iso(_utcnow()), watcher_id),
            )
        finally:
            conn.close()

    def set_status(self, watcher_id: str, status: str, last_error: str = "", matched_snippet: str = "") -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                "UPDATE ScreenWatchers SET Status = ?, LastError = ?, MatchedSnippet = ?, UpdatedAtUtc = ? WHERE WatcherId = ?",
                (status, last_error, matched_snippet, _iso(_utcnow()), watcher_id),
            )
        finally:
            conn.close()


def _row_to_watcher(row) -> ScreenWatcherEntity:
    return ScreenWatcherEntity(
        watcher_id=row["WatcherId"],
        process_name=row["ProcessName"],
        window_title_hint=row["WindowTitleHint"],
        app_display_name=row["AppDisplayName"],
        watch_text=row["WatchText"],
        action_kind=row["ActionKind"],
        whatsapp_chat=row["WhatsAppChat"],
        whatsapp_message=row["WhatsAppMessage"],
        poll_interval_seconds=row["PollIntervalSeconds"],
        is_enabled=bool(row["IsEnabled"]),
        status=row["Status"],
        last_error=row["LastError"],
        matched_snippet=row["MatchedSnippet"],
        created_at_utc=datetime.fromisoformat(row["CreatedAtUtc"]),
        updated_at_utc=datetime.fromisoformat(row["UpdatedAtUtc"]),
    )


# (process_name, window_title_hint) -> window handle, or None if the app isn't open.
FindWindow = Callable[[str, str], Optional[int]]
# window handle -> (ok, text-or-error). Synchronous; the service runs it off-loop.
ReadScreen = Callable[[int], tuple[bool, str]]
# (chat, message) -> (ok, detail)
SendWhatsApp = Callable[[str, str], Awaitable[tuple[bool, str]]]


class ScreenWatchService:
    """Runs the watcher poll loops. Same scheduler as the WhatsApp relay (one
    asyncio task per watcher, staggered, skip-if-busy); this service is what
    each tick calls."""

    def __init__(
        self,
        repository: ScreenWatcherRepository,
        scheduler,
        find_window: FindWindow,
        read_screen: ReadScreen,
        ai_config_provider: Optional[Callable[[], tuple[str, str, str]]] = None,
        send_whatsapp: Optional[SendWhatsApp] = None,
    ) -> None:
        self._repository = repository
        self._scheduler = scheduler
        self._find_window = find_window
        self._read_screen = read_screen
        self._ai_config_provider = ai_config_provider
        self._send_whatsapp = send_whatsapp
        self._notification_handlers: list[Callable[[str, str], None]] = []
        self._activity_handlers: list[Callable[[str, str, str], None]] = []
        # Last seen screen-text hash per watcher: when the screen hasn't
        # changed, the match result can't have changed either — skip the work
        # (and, crucially, the AI call).
        self._last_text_hash: dict[str, str] = {}

        scheduler.set_binding_poll_requested_handler(self._on_poll_requested)
        scheduler.set_relay_enabled(True)  # per-watcher gating comes from sync_async

    def on_notification(self, handler: Callable[[str, str], None]) -> None:
        """handler(title, body) — fired when a watcher matches (or fails)."""
        self._notification_handlers.append(handler)

    def on_activity(self, handler: Callable[[str, str, str], None]) -> None:
        """handler(app, kind, detail) with kinds watch_matched / watch_error."""
        self._activity_handlers.append(handler)

    async def start_async(self) -> None:
        await self.sync_async()

    async def sync_async(self) -> None:
        self._scheduler.sync_bindings(self._repository.get_watchers())

    def get_watchers(self) -> list[ScreenWatcherEntity]:
        return self._repository.get_watchers()

    async def add_watcher_async(self, watcher: ScreenWatcherEntity) -> None:
        self._repository.upsert_watcher(replace(watcher, status="watching"))
        await self.sync_async()

    async def set_enabled_async(self, watcher_id: str, enabled: bool) -> None:
        self._repository.set_enabled(watcher_id, enabled)
        self._last_text_hash.pop(watcher_id, None)  # re-armed: evaluate fresh
        await self.sync_async()

    async def delete_watcher_async(self, watcher_id: str) -> None:
        self._scheduler.stop_binding(watcher_id)
        self._repository.delete_watcher(watcher_id)
        self._last_text_hash.pop(watcher_id, None)
        await self.sync_async()

    async def _on_poll_requested(self, watcher_id: str) -> None:
        try:
            await self.poll_watcher_async(watcher_id)
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            logger.warning("Screen watcher poll failed for %s", watcher_id, exc_info=True)

    async def poll_watcher_async(self, watcher_id: str) -> None:
        watcher = self._repository.get_watcher(watcher_id)
        if watcher is None or not watcher.is_enabled:
            return

        handle = self._find_window(watcher.process_name, watcher.window_title_hint)
        if handle is None:
            self._repository.set_status(watcher.watcher_id, "app-not-open")
            self._last_text_hash.pop(watcher.watcher_id, None)
            return

        ok, text = await asyncio.to_thread(self._read_screen, handle)
        if not ok:
            self._repository.set_status(watcher.watcher_id, "error", last_error=text)
            self._record_activity(watcher.group_name, "watch_error", text)
            return

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self._last_text_hash.get(watcher.watcher_id) == digest:
            return  # screen unchanged since last check — result can't differ
        self._last_text_hash[watcher.watcher_id] = digest

        matched, snippet = await self._matches(watcher.watch_text, text)
        if matched:
            await self._fire(watcher, snippet)
        elif watcher.status != "watching":
            self._repository.set_status(watcher.watcher_id, "watching")

    async def _matches(self, watch_text: str, screen_text: str) -> tuple[bool, str]:
        if trigger_match.literal_match(watch_text, screen_text):
            return True, _snippet_around(watch_text, screen_text)

        api_key, model, base_url = self._ai_config()
        if api_key:
            verdict = await openai_client.classify_intent_match_async(
                api_key, model, watch_text, screen_text[:_MAX_AI_TEXT_CHARS], base_url=base_url
            )
            if verdict:
                return True, ""
        return False, ""

    async def _fire(self, watcher: ScreenWatcherEntity, snippet: str) -> None:
        app = watcher.group_name
        detail = snippet or watcher.watch_text

        if watcher.action_kind == "whatsapp" and self._send_whatsapp is not None:
            message = watcher.whatsapp_message.strip() or f"winSpark: “{watcher.watch_text}” appeared in {app}."
            ok, send_detail = await self._send_whatsapp(watcher.whatsapp_chat, message)
            if not ok:
                # The condition DID appear — don't silently lose that because the
                # send failed. Surface both, and still one-shot the watcher so it
                # doesn't hammer a failing send path.
                self._repository.set_enabled(watcher.watcher_id, False)
                self._repository.set_status(watcher.watcher_id, "error", last_error=send_detail, matched_snippet=snippet)
                self._notify(f"{app}: found it, but the WhatsApp message failed", send_detail)
                self._record_activity(app, "watch_error", f"matched, but sending failed — {send_detail}")
                await self.sync_async()
                return
            self._notify(f"{app}: found it", f"Sent your WhatsApp message to {watcher.whatsapp_chat}.")
        else:
            self._notify(f"{app}: found it", f"“{detail}” appeared on screen.")

        self._repository.set_enabled(watcher.watcher_id, False)
        self._repository.set_status(watcher.watcher_id, "matched", matched_snippet=snippet)
        self._record_activity(app, "watch_matched", detail)
        await self.sync_async()

    def _ai_config(self) -> tuple[str, str, str]:
        if self._ai_config_provider is None:
            return "", "", ""
        try:
            api_key, model, base_url = self._ai_config_provider()
        except Exception:  # noqa: BLE001
            return "", "", ""
        return (api_key or "").strip(), (model or "").strip(), base_url or ""

    def _notify(self, title: str, body: str) -> None:
        for handler in self._notification_handlers:
            try:
                handler(title, body)
            except Exception:  # noqa: BLE001
                logger.warning("watch notification handler failed", exc_info=True)

    def _record_activity(self, app: str, kind: str, detail: str) -> None:
        for handler in self._activity_handlers:
            try:
                handler(app, kind, detail)
            except Exception:  # noqa: BLE001
                logger.warning("watch activity handler failed", exc_info=True)


def _snippet_around(needle: str, haystack: str, radius: int = 40) -> str:
    """A little context around the literal match, for the notification."""
    index = haystack.lower().find(needle.strip().lower())
    if index < 0:
        words = needle.strip().split()
        index = haystack.lower().find(words[0].lower()) if words else -1
    if index < 0:
        return needle.strip()
    start = max(0, index - radius)
    end = min(len(haystack), index + len(needle) + radius)
    return haystack[start:end].strip()
