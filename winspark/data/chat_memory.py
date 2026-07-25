"""Per-chat conversation memory — the recent back-and-forth an AI reply draws
on so it follows the thread instead of answering each message cold.

Two interchangeable stores behind one surface:

- The SQLite repository (the default) already implements append/get/clear/list,
  so it IS a valid store as-is — no wrapper needed.
- ``MongoChatMemoryStore`` keeps the same memory in a MongoDB collection, for
  when you want it in your own database (shared across machines, queryable,
  outside the app's local SQLite file).

The host picks the store: MongoDB when a connection string is configured AND
reachable, otherwise SQLite — so a missing/misconfigured Mongo never stops the
app, it just falls back. ``build_chat_memory_store`` encapsulates that choice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class ChatMemoryStore(Protocol):
    def append_chat_memory(self, group_name: str, role: str, sender: str, text: str, keep: int = 24,
                           *, media_kind: str = "", media_note: str = "", media_path: str = "",
                           time_text: str = "") -> None: ...
    def get_chat_memory(self, group_name: str, limit: int = 24) -> list[tuple[str, str, str]]: ...
    def get_chat_memory_rich(self, group_name: str, limit: int = 24) -> list[dict]: ...
    def clear_chat_memory(self, group_name: str) -> None: ...
    def get_chats_with_memory(self) -> list[tuple[str, int]]: ...


class MongoChatMemoryStore:
    """A MongoDB-backed chat memory. One document per remembered message in a
    single collection, keyed by chat name; ``append`` trims each chat to its
    newest ``keep`` messages so a chat's memory stays bounded, matching the
    SQLite store's behaviour exactly.

    Construction pings the server (short timeout) so a dead/misconfigured URI
    fails fast and the caller can fall back to SQLite, rather than hanging the
    first append."""

    def __init__(self, uri: str, database: str = "winspark", collection: str = "chat_memory") -> None:
        from pymongo import MongoClient  # imported lazily: optional dependency

        # serverSelectionTimeoutMS keeps a wrong/unreachable URI from blocking
        # for the default 30s — we want a quick "nope, fall back to SQLite".
        self._client = MongoClient(uri, serverSelectionTimeoutMS=1500)
        self._client.admin.command("ping")  # raises if the server isn't reachable
        self._col = self._client[database][collection]
        self._col.create_index([("group", 1), ("seq", -1)])
        logger.info("Chat memory using MongoDB at %s (db=%s)", _redact(uri), database)

    def append_chat_memory(self, group_name: str, role: str, sender: str, text: str, keep: int = 24,
                           *, media_kind: str = "", media_note: str = "", media_path: str = "",
                           time_text: str = "") -> None:
        # MongoDB is the durable, UNBOUNDED store: it keeps a chat's FULL history
        # (never trims), so RAG can retrieve from the whole conversation and
        # nothing is ever lost. `keep` is accepted for interface parity with the
        # bounded local SQLite store but is deliberately ignored here.
        group = (group_name or "").strip()
        body = (text or "").strip()
        if not group or not body:
            return
        # A per-chat monotonic sequence gives a stable oldest->newest order
        # without depending on clock resolution when messages arrive fast.
        last = self._col.find_one({"group": group}, sort=[("seq", -1)], projection={"seq": 1})
        seq = (last["seq"] + 1) if last else 0
        self._col.insert_one({
            "group": group, "seq": seq, "role": role, "sender": sender or "",
            "text": body, "media_kind": media_kind or "", "media_note": media_note or "",
            "media_path": media_path or "", "time_text": time_text or "",
            "created_utc": datetime.now(timezone.utc).isoformat(),
        })

    def get_chat_memory(self, group_name: str, limit: int = 24) -> list[tuple[str, str, str]]:
        group = (group_name or "").strip()
        rows = list(self._col.find({"group": group}, sort=[("seq", -1)]).limit(max(1, limit)))
        rows.reverse()  # oldest first
        return [(r.get("role", ""), r.get("sender", ""), r.get("text", "")) for r in rows]

    def get_chat_memory_rich(self, group_name: str, limit: int = 24) -> list[dict]:
        group = (group_name or "").strip()
        rows = list(self._col.find({"group": group}, sort=[("seq", -1)]).limit(max(1, limit)))
        rows.reverse()  # oldest first
        return [
            {"role": r.get("role", ""), "sender": r.get("sender", ""), "text": r.get("text", ""),
             "media_kind": r.get("media_kind", ""), "media_note": r.get("media_note", ""),
             "media_path": r.get("media_path", ""), "time_text": r.get("time_text", "")}
            for r in rows
        ]

    def clear_chat_memory(self, group_name: str) -> None:
        self._col.delete_many({"group": (group_name or "").strip()})

    def get_chats_with_memory(self) -> list[tuple[str, int]]:
        pipeline = [
            {"$group": {"_id": "$group", "count": {"$sum": 1}, "last": {"$max": "$seq"}}},
            {"$sort": {"last": -1}},
        ]
        return [(d["_id"], d["count"]) for d in self._col.aggregate(pipeline)]

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def _redact(uri: str) -> str:
    """Hide credentials in a Mongo URI before logging it (mongodb://user:pass@host)."""
    if "@" in uri and "//" in uri:
        scheme, rest = uri.split("//", 1)
        if "@" in rest:
            return f"{scheme}//***@{rest.split('@', 1)[1]}"
    return uri


class MirroredChatMemoryStore:
    """Keeps chat memory in a primary store (MongoDB) AND a local mirror
    (SQLite) at once, so the two never diverge — the fix for "messages saved
    to MongoDB don't show in the local store, and vice versa".

    Every write goes to BOTH. Reads prefer the primary (so you're genuinely
    using your MongoDB) but fall back to the mirror if the primary is briefly
    unreachable, and the mirror is a durable local backup that always works.
    `reconcile()` copies each store's chats into the other so nothing is
    hidden right after you connect."""

    def __init__(self, primary: ChatMemoryStore, mirror: ChatMemoryStore) -> None:
        self.primary = primary
        self.mirror = mirror

    def append_chat_memory(self, group_name: str, role: str, sender: str, text: str, keep: int = 24,
                           *, media_kind: str = "", media_note: str = "", media_path: str = "",
                           time_text: str = "") -> None:
        # Both, independently: a failure in one must not skip the other, so a
        # blip in MongoDB never costs the local copy (and vice versa).
        for store in (self.mirror, self.primary):
            try:
                store.append_chat_memory(group_name, role, sender, text, keep=keep,
                                         media_kind=media_kind, media_note=media_note,
                                         media_path=media_path, time_text=time_text)
            except Exception:  # noqa: BLE001
                logger.warning("chat memory append to %s failed", type(store).__name__, exc_info=True)

    def get_chat_memory(self, group_name: str, limit: int = 24) -> list[tuple[str, str, str]]:
        try:
            return self.primary.get_chat_memory(group_name, limit)
        except Exception:  # noqa: BLE001 - MongoDB down mid-session: serve the local copy
            logger.warning("reading chat memory from primary failed — using local mirror", exc_info=True)
            return self.mirror.get_chat_memory(group_name, limit)

    def get_chat_memory_rich(self, group_name: str, limit: int = 24) -> list[dict]:
        try:
            return self.primary.get_chat_memory_rich(group_name, limit)
        except Exception:  # noqa: BLE001 - MongoDB down mid-session: serve the local copy
            logger.warning("reading rich chat memory from primary failed — using local mirror", exc_info=True)
            return self.mirror.get_chat_memory_rich(group_name, limit)

    def clear_chat_memory(self, group_name: str) -> None:
        for store in (self.primary, self.mirror):
            try:
                store.clear_chat_memory(group_name)
            except Exception:  # noqa: BLE001
                logger.warning("clearing chat memory in %s failed", type(store).__name__, exc_info=True)

    def get_chats_with_memory(self) -> list[tuple[str, int]]:
        try:
            return self.primary.get_chats_with_memory()
        except Exception:  # noqa: BLE001
            return self.mirror.get_chats_with_memory()

    def reconcile(self) -> int:
        """Make both stores hold the union of each other's chats (a chat present
        in only one is copied to the other; chats in both are left as-is).
        Returns how many messages were copied INTO the primary — i.e. carried
        up from local into MongoDB — for the "moved N over" confirmation."""
        into_primary = copy_chat_memory(self.mirror, self.primary)
        copy_chat_memory(self.primary, self.mirror)
        return into_primary

    def close(self) -> None:
        for store in (self.primary, self.mirror):
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()


def copy_chat_memory(src: ChatMemoryStore, dst: ChatMemoryStore, keep: int = 24) -> int:
    """Copy every chat's remembered messages from `src` into `dst`, so nothing
    is left behind when the user switches storage backends. Chats `dst` already
    has are skipped, so reconnecting doesn't duplicate. Returns the number of
    messages copied. Never raises — a copy failure just means fewer messages
    migrated, not a broken switch."""
    copied = 0
    try:
        already = {group for group, _ in dst.get_chats_with_memory()}
        for group, _count in src.get_chats_with_memory():
            if group in already:
                continue
            for row in src.get_chat_memory_rich(group, limit=100_000):
                dst.append_chat_memory(
                    group, row.get("role", ""), row.get("sender", ""), row.get("text", ""), keep=keep,
                    media_kind=row.get("media_kind", ""), media_note=row.get("media_note", ""),
                    media_path=row.get("media_path", ""), time_text=row.get("time_text", ""),
                )
                copied += 1
    except Exception:  # noqa: BLE001
        logger.warning("copying chat memory between stores failed partway", exc_info=True)
    return copied


def build_chat_memory_store(uri: str, sqlite_fallback: ChatMemoryStore,
                            database: str = "winspark") -> ChatMemoryStore:
    """When `uri` is set and the server answers, return a store that MIRRORS
    to both MongoDB and the local SQLite `sqlite_fallback`, so the two never
    diverge; otherwise return the SQLite fallback alone. Never raises: any
    Mongo problem logs a warning and falls back, so chat memory always works
    even if MongoDB is down."""
    uri = (uri or "").strip()
    if not uri:
        return sqlite_fallback
    try:
        mongo = MongoChatMemoryStore(uri, database=database or "winspark")
    except Exception as ex:  # noqa: BLE001 - pymongo missing, unreachable, auth, etc.
        logger.warning("MongoDB chat memory unavailable (%s) — using local storage instead", ex)
        return sqlite_fallback
    return MirroredChatMemoryStore(primary=mongo, mirror=sqlite_fallback)
