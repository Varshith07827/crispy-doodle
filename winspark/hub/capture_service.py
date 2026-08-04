"""Read & store: save a chat's messages to MongoDB as they arrive.

Independent of the send flow by design — turning capture on for a chat has
nothing to do with whether that chat is linked to a webhook, and neither
service knows about the other.

How it works, and the honest limit: WhatsApp's accessibility tree only exposes
the conversation that is currently OPEN. So this reads whatever chat is open,
and stores it only if that chat has capture switched on. A chat you enabled but
aren't looking at is not being captured — nothing can capture it, short of
opening it (which would hijack the mouse) or a protocol client.

Storage is MongoDB only, no local copy. A write that fails is a message lost,
so `last_error` carries the reason for the UI to show instead of looking idle.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from winspark.hub.message_hub import HubMessage
from winspark.hub.settings_files import HubSettings

logger = logging.getLogger(__name__)


@dataclass
class CaptureStats:
    """What the UI shows about the last pass."""

    last_chat: str = ""
    stored_total: int = 0
    failed_total: int = 0
    last_error: str = ""
    skipped_reason: str = ""


class CaptureService:
    """Saves the open conversation to the hub, one pass per `tick()`.

    Dependencies are injected rather than constructed so this can be tested
    without WhatsApp or a Mongo server:

    `read_open_conversation()` -> (chat_name, [WhatsAppMessage])
    `store_provider()`         -> a MessageHubStore, or None if unavailable
    """

    def __init__(self, settings: HubSettings, read_open_conversation, store_provider) -> None:
        self._settings = settings
        self._read = read_open_conversation
        self._store_provider = store_provider
        self._lock = threading.Lock()
        self.stats = CaptureStats()

    def tick(self) -> int:
        """One capture pass. Returns how many messages were stored this pass.

        Never raises: this runs on a timer, and a bad read or a network blip
        must not stop the next pass from trying."""
        try:
            return self._tick()
        except Exception as ex:  # noqa: BLE001
            logger.warning("capture pass failed", exc_info=True)
            with self._lock:
                self.stats.last_error = str(ex)
            return 0

    def _tick(self) -> int:
        enabled = self._settings.capturing_chats()
        if not enabled:
            self._note_skip("No chats are set to save.")
            return 0

        chat, messages = self._read()
        chat = (chat or "").strip()
        if not chat:
            self._note_skip("No conversation is open in WhatsApp.")
            return 0
        if not self._settings.is_capturing(chat):
            # Reading is unavoidable — we can't know which chat is open without
            # looking — but nothing is stored for a chat that wasn't opted in.
            self._note_skip(f"“{chat}” isn't set to save.")
            return 0

        store = self._store_provider()
        if store is None:
            self._note_skip("MongoDB isn't connected.")
            return 0

        usable = [m for m in messages if (getattr(m, "text", "") or "").strip()]
        if not usable:
            self._note_skip(f"Nothing to save in “{chat}” yet.")
            return 0

        # Every message on screen is offered every pass; the store upserts on a
        # fingerprint, so re-offering one already saved costs nothing and can't
        # duplicate it. That's why there's no "what did I send last time" state
        # to keep here — the idempotent write IS the deduplication.
        stored, failed = store.save_many(HubMessage.from_whatsapp(chat, m) for m in usable)
        with self._lock:
            self.stats.last_chat = chat
            self.stats.stored_total += stored
            self.stats.failed_total += failed
            self.stats.last_error = store.last_error if failed else ""
            self.stats.skipped_reason = ""
        return stored

    def _note_skip(self, reason: str) -> None:
        with self._lock:
            self.stats.skipped_reason = reason

    def status_line(self) -> str:
        """One line for the UI."""
        with self._lock:
            stats = self.stats
            if stats.last_error:
                return f"Not saving — {stats.last_error}"
            if stats.skipped_reason and not stats.stored_total:
                return stats.skipped_reason
            saved = f"Saved {stats.stored_total} message{'s' if stats.stored_total != 1 else ''}"
            where = f" from “{stats.last_chat}”" if stats.last_chat else ""
            lost = f" ({stats.failed_total} could not be saved)" if stats.failed_total else ""
            return saved + where + lost
