"""Send: poll a chat's linked webhook and deliver whatever it returns.

Independent of the capture flow — a chat can be linked here without being saved
to MongoDB, and vice versa.

One link per chat: refresh the list, pick a chat, paste a webhook URL, and this
polls it on an interval (3 seconds by default, which is also the floor). A
non-empty response is sent to that chat.

Two properties worth stating, both learned from the older relay:

- **A slow send must not stack up.** Sending drives WhatsApp's real UI and can
  take seconds; a link already sending is skipped rather than queued, so a slow
  chat can't build a backlog of duplicate deliveries.
- **The same text twice is two messages.** The webhook is a queue, not a state:
  if it returns "ping" twice, that's two things to send. Only an in-flight send
  is skipped, never a repeat.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from winspark.hub.settings_files import HubSettings, SendLink

logger = logging.getLogger(__name__)


@dataclass
class LinkStatus:
    """Per-chat state the UI shows next to a link."""

    chat: str
    last_polled: float = 0.0
    polls: int = 0
    sent: int = 0
    last_error: str = ""
    sending: bool = False


@dataclass
class SpoolStats:
    links: dict = field(default_factory=dict)   # chat (lowercased) -> LinkStatus


class SpoolService:
    """Polls every enabled link whose interval is due, one pass per `tick()`.

    Injected dependencies keep this testable with no network and no WhatsApp:

    `fetch(url)`          -> str, the text to send ("" for nothing waiting)
    `send(chat, text)`    -> (ok: bool, detail: str)
    """

    def __init__(self, settings: HubSettings, fetch, send) -> None:
        self._settings = settings
        self._fetch = fetch
        self._send = send
        self._lock = threading.Lock()
        self._stats = SpoolStats()

    def status_for(self, chat: str) -> LinkStatus:
        key = (chat or "").strip().lower()
        with self._lock:
            return self._stats.links.get(key) or LinkStatus(chat=chat)

    def _status(self, chat: str) -> LinkStatus:
        key = chat.strip().lower()
        status = self._stats.links.get(key)
        if status is None:
            status = LinkStatus(chat=chat)
            self._stats.links[key] = status
        return status

    def tick(self, now: float | None = None) -> int:
        """Poll every link that's due. Returns how many messages were sent."""
        now = time.monotonic() if now is None else now
        sent = 0
        for link in self._settings.enabled_send_links():
            try:
                sent += self._poll_link(link, now)
            except Exception as ex:  # noqa: BLE001 - one bad link must not stop the rest
                logger.warning("spool poll for %s failed", link.chat, exc_info=True)
                with self._lock:
                    self._status(link.chat).last_error = str(ex)
        return sent

    def _poll_link(self, link: SendLink, now: float) -> int:
        with self._lock:
            status = self._status(link.chat)
            if status.sending:
                # Already delivering. Skipping (not queueing) is what stops a
                # slow chat building a backlog it then floods the chat with.
                return 0
            if status.last_polled and now - status.last_polled < link.interval_seconds:
                return 0
            status.last_polled = now
            status.polls += 1
            status.sending = True

        try:
            text = (self._fetch(link.webhook_url) or "").strip()
            if not text:
                with self._lock:
                    self._status(link.chat).last_error = ""
                return 0
            ok, detail = self._send(link.chat, text)
            with self._lock:
                status = self._status(link.chat)
                if ok:
                    status.sent += 1
                    status.last_error = ""
                else:
                    status.last_error = detail or "Could not send."
            return 1 if ok else 0
        finally:
            with self._lock:
                self._status(link.chat).sending = False

    def status_line(self, chat: str) -> str:
        """One line for the UI, for one chat."""
        link = self._settings.send_link_for(chat)
        if link is None:
            return "Not linked to a webhook."
        if not link.webhook_url:
            return "No webhook address set."
        status = self.status_for(chat)
        if status.last_error:
            return f"Problem: {status.last_error}"
        if not link.enabled:
            return f"Paused — {status.sent} sent so far."
        every = f"every {link.interval_seconds}s"
        return f"Watching {every} — {status.sent} sent, {status.polls} checks."
