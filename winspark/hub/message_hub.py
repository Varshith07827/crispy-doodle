"""Writing WhatsApp messages straight to MongoDB, as they arrive.

Deliberately NOT the chat-memory store:

- It writes to **wa_message_hub**, not `chat_memory`. That collection belongs to
  the older per-chat AI memory, which is on its way out; sharing it would tie
  this hub's data to that feature's lifetime.
- There is **no local mirror**. Chat memory keeps a SQLite copy and reconciles;
  this is MongoDB or nothing, so a message either lands in the remote database
  or is reported as not landing. Nothing is silently held somewhere else.
- Writes are **idempotent**. The reader re-reads whatever is on screen every few
  seconds, so the same message is offered over and over. Each one carries a
  fingerprint and is upserted on it, which makes re-capture free instead of the
  source of duplicates the append-only store suffered from.

Because there is no mirror, an unreachable server means messages are LOST, not
delayed. `last_error` is kept so the UI can say so rather than looking idle.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def message_fingerprint(chat: str, direction: str, sender: str, text: str, time_text: str) -> str:
    """Stable id for one message, used to upsert instead of append.

    Includes the message's own clock label, so the same words said twice at
    different times are two messages — the mistake the chat-memory store made,
    where identity was the text alone and a repeated question was discarded as a
    duplicate for ever."""
    raw = "␟".join([
        (chat or "").strip().lower(),
        (direction or "").strip().lower(),
        (sender or "").strip().lower(),
        (text or "").strip(),
        (time_text or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HubMessage:
    """One WhatsApp message, in the shape it's stored."""

    chat: str
    direction: str          # "in" (received) or "out" (sent by us)
    text: str
    sender: str = ""
    media_kind: str = ""    # photo / voice / video / document / sticker / gif
    media_note: str = ""    # duration, filename, or caption
    time_text: str = ""     # the bubble's own label, e.g. "1:54 pm"

    @staticmethod
    def from_whatsapp(chat: str, message) -> "HubMessage":
        """Build from a connector WhatsAppMessage."""
        return HubMessage(
            chat=chat,
            direction="in" if getattr(message, "is_incoming", True) else "out",
            text=getattr(message, "text", "") or "",
            sender=(getattr(message, "sender", "") or "") if getattr(message, "is_incoming", True) else "",
            media_kind=getattr(message, "media_kind", "") or "",
            media_note=getattr(message, "media_note", "") or "",
            time_text=getattr(message, "time_text", "") or "",
        )

    @property
    def fingerprint(self) -> str:
        return message_fingerprint(self.chat, self.direction, self.sender, self.text, self.time_text)

    def to_document(self) -> dict:
        return {
            "chat": self.chat,
            "direction": self.direction,
            "sender": self.sender,
            "text": self.text,
            "media_kind": self.media_kind,
            "media_note": self.media_note,
            "message_time": self.time_text,
            "fingerprint": self.fingerprint,
        }


class MessageHubUnavailableError(RuntimeError):
    """MongoDB isn't usable — no pymongo, or the server won't answer."""


class MessageHubStore:
    """The wa_message_hub collection. Connect once, then `save` per message."""

    def __init__(self, uri: str, database: str = "", collection: str = "wa_message_hub",
                 timeout_ms: int = 8000) -> None:
        from winspark.data.chat_memory import resolve_database, uses_tls

        try:
            from pymongo import ASCENDING, MongoClient
        except ImportError as ex:  # pragma: no cover - optional dependency
            raise MessageHubUnavailableError(
                "the 'pymongo' package is required — run: pip install \"pymongo[srv]\"") from ex

        options: dict = {"serverSelectionTimeoutMS": timeout_ms, "connectTimeoutMS": timeout_ms}
        if uses_tls(uri):
            try:
                import certifi

                options["tlsCAFile"] = certifi.where()
            except ImportError:
                pass

        self.database_name = resolve_database(uri, database)
        self.collection_name = (collection or "wa_message_hub").strip() or "wa_message_hub"
        self._client = MongoClient(uri, **options)
        self._client.admin.command("ping")   # fail here, not on the first message
        self._col = self._client[self.database_name][self.collection_name]
        # Unique on the fingerprint so a re-read can never duplicate a message,
        # even if two capture passes race.
        self._col.create_index([("fingerprint", ASCENDING)], unique=True)
        self._col.create_index([("chat", ASCENDING), ("captured_utc", ASCENDING)])
        self._lock = threading.Lock()
        self.last_error = ""
        self.saved_count = 0
        logger.info("Message hub using %s.%s", self.database_name, self.collection_name)

    def save(self, message: HubMessage) -> bool:
        """Store one message. True if it reached MongoDB (or was already there).

        Never raises: a capture pass must not die because the network blinked.
        False means the message did NOT land — and with no local mirror, it is
        gone, so callers should surface `last_error` rather than assume success.
        """
        if not (message.chat or "").strip() or not (message.text or "").strip():
            return False
        document = message.to_document()
        try:
            self._col.update_one(
                {"fingerprint": document["fingerprint"]},
                {
                    "$set": document,
                    "$setOnInsert": {"captured_utc": datetime.now(timezone.utc).isoformat()},
                },
                upsert=True,
            )
        except Exception as ex:  # noqa: BLE001 - report, never propagate
            with self._lock:
                self.last_error = str(ex)
            logger.warning("saving a message to the hub failed: %s", ex)
            return False
        with self._lock:
            self.last_error = ""
            self.saved_count += 1
        return True

    def save_many(self, messages) -> tuple[int, int]:
        """Save a batch, returning (stored, failed)."""
        stored = failed = 0
        for message in messages:
            if self.save(message):
                stored += 1
            else:
                failed += 1
        return stored, failed

    def count_for(self, chat: str) -> int:
        try:
            return self._col.count_documents({"chat": (chat or "").strip()})
        except Exception:  # noqa: BLE001
            return 0

    def recent(self, chat: str, limit: int = 50) -> list[dict]:
        try:
            rows = list(self._col.find({"chat": (chat or "").strip()},
                                       sort=[("captured_utc", -1)]).limit(max(1, limit)))
        except Exception:  # noqa: BLE001
            return []
        rows.reverse()
        for row in rows:
            row.pop("_id", None)
        return rows

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def build_message_hub(uri: str, database: str = "", collection: str = "wa_message_hub"):
    """(store, problem) — `store` is None when MongoDB can't be used, with
    `problem` explaining why in the words the Settings panel already uses."""
    from winspark.data.chat_memory import describe_connection_problem

    uri = (uri or "").strip()
    if not uri:
        return None, "No MongoDB connection string set."
    try:
        return MessageHubStore(uri, database=database, collection=collection), ""
    except Exception as ex:  # noqa: BLE001
        problem = describe_connection_problem(ex, uri)
        logger.warning("message hub unavailable: %s", problem)
        return None, problem
