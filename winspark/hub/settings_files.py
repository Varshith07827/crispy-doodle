"""Configuration for the message hub, in two JSON files instead of a database.

    config.json   what you set once: the MongoDB connection, the collection to
                  write into, the default spool interval.
    data.json     what changes as you use it: which chats are linked to a
                  webhook, which chats are being saved to MongoDB.

Split that way on purpose. `config.json` is the file you'd hand-edit or copy to
another machine; `data.json` is churn, rewritten whenever you link or unlink a
chat. Mixing them would mean an editing mistake in the connection string can be
overwritten by a UI action a second later.

There is no database behind this, which changes what "careful" means: a
half-written file is not recoverable from anywhere. So every write goes to a
temporary file and is then atomically replaced, and every read tolerates the
file being absent, empty, corrupt or the wrong shape by falling back to
defaults. Losing your settings must never be worse than starting fresh.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The collection every message is written to. Deliberately NOT "chat_memory":
# that belongs to the older per-chat AI memory, which is on its way out, and
# sharing a collection with it would tie this hub's lifetime to that feature's.
DEFAULT_COLLECTION = "wa_message_hub"

# Matches FetchWebhookDefaults.MIN_POLL_INTERVAL_SECONDS — polling a webhook
# faster than this buys nothing and hammers whatever is behind it.
DEFAULT_SPOOL_SECONDS = 3
MIN_SPOOL_SECONDS = 3


@dataclass(frozen=True, slots=True)
class HubConfig:
    """config.json — the connection and the defaults."""

    mongo_uri: str = ""
    # Blank means "whatever database the connection string names", which is how
    # every other Mongo tool reads that string.
    mongo_database: str = ""
    mongo_collection: str = DEFAULT_COLLECTION
    spool_interval_seconds: int = DEFAULT_SPOOL_SECONDS

    @staticmethod
    def from_dict(raw: dict) -> "HubConfig":
        collection = str(raw.get("mongo_collection") or "").strip() or DEFAULT_COLLECTION
        try:
            interval = int(raw.get("spool_interval_seconds") or DEFAULT_SPOOL_SECONDS)
        except (TypeError, ValueError):
            interval = DEFAULT_SPOOL_SECONDS
        return HubConfig(
            mongo_uri=str(raw.get("mongo_uri") or "").strip(),
            mongo_database=str(raw.get("mongo_database") or "").strip(),
            mongo_collection=collection,
            spool_interval_seconds=max(MIN_SPOOL_SECONDS, interval),
        )


@dataclass(frozen=True, slots=True)
class SendLink:
    """One chat wired to a webhook the spool service polls."""

    chat: str
    webhook_url: str = ""
    enabled: bool = False
    interval_seconds: int = DEFAULT_SPOOL_SECONDS

    @staticmethod
    def from_dict(raw: dict) -> Optional["SendLink"]:
        chat = str(raw.get("chat") or "").strip()
        if not chat:
            return None
        try:
            interval = int(raw.get("interval_seconds") or DEFAULT_SPOOL_SECONDS)
        except (TypeError, ValueError):
            interval = DEFAULT_SPOOL_SECONDS
        return SendLink(
            chat=chat,
            webhook_url=str(raw.get("webhook_url") or "").strip(),
            enabled=bool(raw.get("enabled")),
            interval_seconds=max(MIN_SPOOL_SECONDS, interval),
        )


@dataclass(frozen=True, slots=True)
class CaptureChat:
    """One chat whose messages are saved to MongoDB as they arrive."""

    chat: str
    enabled: bool = False

    @staticmethod
    def from_dict(raw: dict) -> Optional["CaptureChat"]:
        chat = str(raw.get("chat") or "").strip()
        if not chat:
            return None
        return CaptureChat(chat=chat, enabled=bool(raw.get("enabled")))


@dataclass(frozen=True, slots=True)
class HubData:
    """data.json — per-chat state, rewritten as the user links and unlinks."""

    send_links: tuple[SendLink, ...] = field(default_factory=tuple)
    capture_chats: tuple[CaptureChat, ...] = field(default_factory=tuple)

    @staticmethod
    def from_dict(raw: dict) -> "HubData":
        def rows(key: str, build):
            values = raw.get(key)
            if not isinstance(values, list):
                return ()
            built = (build(v) for v in values if isinstance(v, dict))
            return tuple(b for b in built if b is not None)

        return HubData(
            send_links=rows("send_links", SendLink.from_dict),
            capture_chats=rows("capture_chats", CaptureChat.from_dict),
        )


def _read_json(path: Path) -> dict:
    """The file's contents as a dict — {} for anything unusable.

    Absent, empty, invalid JSON and "valid JSON but a list" all land here, and
    all mean the same thing to the caller: no usable settings, use defaults.
    Without a database to fall back on, refusing to start over a stray comma
    would be the worse failure."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        logger.warning("%s is unreadable (%s) — using defaults", path.name, ex)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    """Write atomically: full content to a temp file in the same directory, then
    os.replace onto the target. A crash mid-write leaves the previous file
    intact rather than a truncated one — the whole safety net, now that these
    files ARE the storage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class HubSettings:
    """Reads and writes config.json / data.json, in memory and on disk.

    Every mutation persists immediately — there is no "save" step for the user
    to forget, and no in-memory state that outlives a crash."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._config_path = self._dir / "config.json"
        self._data_path = self._dir / "data.json"
        self._lock = threading.Lock()
        self._config = HubConfig.from_dict(_read_json(self._config_path))
        self._data = HubData.from_dict(_read_json(self._data_path))

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def data_path(self) -> Path:
        return self._data_path

    # --- config.json ----------------------------------------------------

    @property
    def config(self) -> HubConfig:
        with self._lock:
            return self._config

    def update_config(self, **changes: Any) -> HubConfig:
        with self._lock:
            merged = HubConfig.from_dict({**asdict(self._config), **changes})
            self._config = merged
            _write_json(self._config_path, asdict(merged))
            return merged

    # --- data.json ------------------------------------------------------

    @property
    def data(self) -> HubData:
        with self._lock:
            return self._data

    def _store_data(self, data: HubData) -> HubData:
        self._data = data
        _write_json(self._data_path, {
            "send_links": [asdict(link) for link in data.send_links],
            "capture_chats": [asdict(chat) for chat in data.capture_chats],
        })
        return data

    def send_link_for(self, chat: str) -> Optional[SendLink]:
        wanted = (chat or "").strip().lower()
        return next((l for l in self.data.send_links if l.chat.strip().lower() == wanted), None)

    def set_send_link(self, chat: str, webhook_url: str, enabled: bool,
                      interval_seconds: Optional[int] = None) -> SendLink:
        """Link a chat to a webhook (one link per chat — re-linking replaces)."""
        chat = (chat or "").strip()
        if not chat:
            raise ValueError("a chat name is required")
        with self._lock:
            interval = interval_seconds if interval_seconds is not None \
                else self._config.spool_interval_seconds
            link = SendLink(chat=chat, webhook_url=(webhook_url or "").strip(),
                            enabled=bool(enabled),
                            interval_seconds=max(MIN_SPOOL_SECONDS, int(interval)))
            others = tuple(l for l in self._data.send_links
                           if l.chat.strip().lower() != chat.lower())
            self._store_data(replace(self._data, send_links=others + (link,)))
            return link

    def remove_send_link(self, chat: str) -> None:
        wanted = (chat or "").strip().lower()
        with self._lock:
            kept = tuple(l for l in self._data.send_links if l.chat.strip().lower() != wanted)
            if len(kept) != len(self._data.send_links):
                self._store_data(replace(self._data, send_links=kept))

    def capture_for(self, chat: str) -> Optional[CaptureChat]:
        wanted = (chat or "").strip().lower()
        return next((c for c in self.data.capture_chats if c.chat.strip().lower() == wanted), None)

    def is_capturing(self, chat: str) -> bool:
        entry = self.capture_for(chat)
        return bool(entry and entry.enabled)

    def set_capture(self, chat: str, enabled: bool) -> CaptureChat:
        chat = (chat or "").strip()
        if not chat:
            raise ValueError("a chat name is required")
        with self._lock:
            entry = CaptureChat(chat=chat, enabled=bool(enabled))
            others = tuple(c for c in self._data.capture_chats
                           if c.chat.strip().lower() != chat.lower())
            self._store_data(replace(self._data, capture_chats=others + (entry,)))
            return entry

    def capturing_chats(self) -> tuple[str, ...]:
        return tuple(c.chat for c in self.data.capture_chats if c.enabled)

    def enabled_send_links(self) -> tuple[SendLink, ...]:
        return tuple(l for l in self.data.send_links if l.enabled and l.webhook_url)
