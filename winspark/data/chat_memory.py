"""Per-chat conversation memory — the recent back-and-forth an AI reply draws
on so it follows the thread instead of answering each message cold.

Two interchangeable stores behind one surface:

- The SQLite repository (the default) already implements append/get/clear/list,
  so it IS a valid store as-is — no wrapper needed.
- ``MongoChatMemoryStore`` keeps the same memory in a MongoDB collection, for
  when you want it in your own database (shared across machines, queryable,
  outside the app's local SQLite file).

Either kind of MongoDB works, and the same connection string field takes both:

- a **local Community Server** — ``mongodb://localhost:27017``
- a **cloud Atlas cluster** — ``mongodb+srv://user:pass@cluster.xxxx.mongodb.net/``

They differ in ways that matter to the connection, so this module adapts rather
than treating one as the other: Atlas needs an SRV DNS lookup plus a TLS
handshake over the internet (seconds), where a local server either answers
instantly or isn't running at all (milliseconds).

The host picks the store: MongoDB when a connection string is configured AND
reachable, otherwise SQLite — so a missing/misconfigured Mongo never stops the
app, it just falls back. ``build_chat_memory_store`` encapsulates that choice.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

DEFAULT_DATABASE = "winspark"

# After the primary fails, how long to stop touching it. Without this, EVERY
# read and write pays the full connect timeout again: a server that has gone
# away turned each chat-memory lookup into an 8-second stall, repeatedly, for
# as long as the app ran. Serving from the local mirror is instant and correct,
# so the only thing gained by retrying immediately is the stall.
PRIMARY_RETRY_SECONDS = 60.0

# Writes made while the primary is unreachable are held here and replayed when
# it comes back, so MongoDB doesn't silently miss whatever was said during an
# outage. Bounded: a long outage must not grow memory without limit. Past this,
# the oldest held writes are dropped — the local mirror still has every one of
# them, so nothing is lost outright, but MongoDB will have a gap.
MAX_PENDING_PRIMARY_WRITES = 500

# How long to wait for a server to answer before falling back to SQLite.
# A local mongod is either up (instant) or absent, so a short timeout keeps the
# app's startup snappy and gives a fast "nope". Atlas has to resolve an SRV
# record and complete a TLS handshake across the internet first — 1.5s there is
# a false negative waiting to happen, especially on a cold/paused cluster.
LOCAL_TIMEOUT_MS = 1500
REMOTE_TIMEOUT_MS = 8000

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}

# MongoDB's own databases: `admin` holds users and roles, `config` the sharding
# metadata, `local` the replication oplog. Application collections do not belong
# in any of them, so a URI naming one is NOT allowed to win over the configured
# database (see resolve_database).
#
# This is not hypothetical. `mongodb://localhost:27017/admin` is the shape you
# get by habit — the auth database really does belong in a connection string,
# but as `?authSource=admin`, not as the path. Trusting the path put 207 real
# chat-memory documents in `admin.chat_memory` on this machine while the
# database the user had actually configured sat empty.
_SYSTEM_DATABASES = {"admin", "local", "config"}


def _strip_credentials(uri: str) -> str:
    """The part of the URI after ``scheme://user:pass@`` — host onwards."""
    return uri.split("://", 1)[-1].rsplit("@", 1)[-1]


def is_srv_uri(uri: str) -> bool:
    """A ``mongodb+srv://`` connection string — the form Atlas hands you."""
    return (uri or "").strip().lower().startswith("mongodb+srv://")


def is_local_uri(uri: str) -> bool:
    """Does this URI point at a server on this machine?"""
    if is_srv_uri(uri):
        return False  # SRV is always a DNS-resolved (i.e. remote) deployment
    host = _strip_credentials((uri or "").strip().lower()).split("/", 1)[0]
    first = host.split(",", 1)[0]  # replica-set seed lists: judge by the first host
    if first.startswith("["):  # bracketed IPv6 literal, e.g. [::1]:27017
        first = first.split("]", 1)[0] + "]"
    else:
        first = first.split(":", 1)[0]
    return first in _LOCAL_HOSTS


def connect_timeout_ms(uri: str) -> int:
    """Server-selection timeout suited to where the server actually is."""
    return LOCAL_TIMEOUT_MS if is_local_uri(uri) else REMOTE_TIMEOUT_MS


def uses_tls(uri: str) -> bool:
    """Is this connection encrypted? Always for Atlas (+srv implies TLS), and
    for anything that asks for it explicitly in the query string."""
    if is_srv_uri(uri):
        return True
    query = (uri or "").lower().partition("?")[2]
    return "tls=true" in query or "ssl=true" in query


def database_in_uri(uri: str) -> str:
    """The database named in the connection string itself, if any — the
    ``/mydb`` in ``mongodb://host:27017/mydb``. Empty when the URI carries none
    (the usual shape of an Atlas string, which ends at ``/?retryWrites=...``)."""
    after_host = _strip_credentials((uri or "").strip()).split("/", 1)
    if len(after_host) < 2:
        return ""
    return after_host[1].partition("?")[0].strip()


def resolve_database(uri: str, configured: str = "") -> str:
    """Which database to actually use.

    A database named IN the URI wins: it is the more specific thing the user
    literally typed, and it is what every other Mongo tool (mongosh, Compass)
    would use for that same string. Silently ignoring it — as this module used
    to — means writes land somewhere the user never asked for. Falls back to
    the configured setting, then to the default name.

    The one exception is a MongoDB system database (`admin`/`local`/`config`):
    those are never a place to put chat memory, and a URI ending `/admin` almost
    always means "authenticate here", not "store here". Such a path is ignored
    in favour of the configured database, which is what the user chose on
    purpose."""
    in_uri = database_in_uri(uri)
    if in_uri.lower() in _SYSTEM_DATABASES:
        in_uri = ""
    return in_uri or (configured or "").strip() or DEFAULT_DATABASE


def describe_connection_problem(ex: Exception, uri: str = "") -> str:
    """Turn a pymongo failure into something a person can act on.

    Deliberately matches on exception *names* rather than importing pymongo's
    types: pymongo is an optional dependency, and the most common failure of
    all is that it isn't installed."""
    name = type(ex).__name__
    text = str(ex)
    low = text.lower()

    if isinstance(ex, ImportError):
        return "The pymongo package isn't installed — run: pip install \"pymongo[srv]\""
    if "dnspython" in low:
        return ("A mongodb+srv:// address needs the dnspython package — "
                "run: pip install \"pymongo[srv]\"")
    if name in ("OperationFailure", "AuthenticationFailed") or "auth failed" in low:
        return "MongoDB rejected the username and password in the connection string."
    if "certificate" in low or ("ssl" in low and "handshake" in low):
        return "The TLS certificate couldn't be verified — run: pip install certifi"
    # DNS trouble arrives wrapped in ConfigurationError, so it must be caught
    # before the generic "that string isn't valid" — the string is usually fine
    # and saying otherwise sends the user off fixing the wrong thing.
    if "resolution lifetime expired" in low or "dns operation timed out" in low:
        return ("Couldn't look up the cluster address (DNS timed out). Check your internet "
                "connection — some networks block the SRV lookups a mongodb+srv:// address needs.")
    if "does not exist" in low or "nxdomain" in low or "no such host" in low:
        return "That cluster address doesn't exist — check the hostname in the connection string."
    if name == "ServerSelectionTimeoutError" or "no servers found" in low:
        if is_srv_uri(uri):
            return ("Couldn't reach the Atlas cluster. Check that this machine's IP is on the "
                    "cluster's Network Access allowlist and that the cluster isn't paused.")
        host = _strip_credentials(uri).split("/", 1)[0] or "the address given"
        return f"No MongoDB server answered at {host} — is mongod running?"
    if name == "ConfigurationError" or name == "InvalidURI":
        return f"That connection string isn't valid: {text}"
    return f"Couldn't connect to MongoDB: {text}"


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

    Works against either a local Community Server or an Atlas cluster — the
    connection string decides, and the timeout adapts to which one it is.

    Construction pings the server so a dead/misconfigured URI fails while the
    caller can still fall back to SQLite, rather than hanging the first
    append."""

    def __init__(self, uri: str, database: str = "", collection: str = "chat_memory") -> None:
        from pymongo import MongoClient  # imported lazily: optional dependency

        # serverSelectionTimeoutMS keeps a wrong/unreachable URI from blocking
        # for the default 30s — we want a "nope, fall back to SQLite" that's
        # quick for a local server but still patient enough for a cloud one.
        timeout_ms = connect_timeout_ms(uri)
        options: dict = {
            "serverSelectionTimeoutMS": timeout_ms,
            # connectTimeoutMS is NOT redundant with the above: for a
            # mongodb+srv:// address pymongo resolves the SRV record inside the
            # MongoClient constructor, BEFORE server selection starts, and that
            # DNS lookup is bounded by connectTimeoutMS alone. Measured without
            # it: an unresolvable Atlas host blocked for 20.6s (dnspython's
            # default lifetime) while serverSelectionTimeoutMS sat unused.
            "connectTimeoutMS": timeout_ms,
        }
        if uses_tls(uri):
            # Python verifies against the OS trust store by default, which is
            # usually fine — but where it isn't (a stripped-down or corporate
            # Windows image), Atlas fails with an opaque certificate error.
            # certifi's bundle is the standard fix; use it when it happens to
            # be installed rather than making it a hard dependency.
            try:
                import certifi

                options["tlsCAFile"] = certifi.where()
            except ImportError:
                pass

        self.database_name = resolve_database(uri, database)
        self._client = MongoClient(uri, **options)
        self._client.admin.command("ping")  # raises if the server isn't reachable
        self._col = self._client[self.database_name][collection]
        self._col.create_index([("group", 1), ("seq", -1)])
        logger.info(
            "Chat memory using MongoDB at %s (db=%s, %s)",
            _redact(uri), self.database_name,
            "Atlas/remote" if not is_local_uri(uri) else "local server",
        )

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
    hidden right after you connect.

    When the primary stops answering it is put aside for `retry_after_seconds`
    rather than retried on every call. Measured on a real server that went
    away: each read spent the full 8-second connect timeout before falling
    back, so every reply the app composed stalled 8 seconds and the log filled
    with identical pymongo tracebacks. Writes made during the outage are held
    and replayed when it comes back, so MongoDB doesn't quietly miss whatever
    was said while it was down."""

    def __init__(self, primary: ChatMemoryStore, mirror: ChatMemoryStore,
                 retry_after_seconds: float = PRIMARY_RETRY_SECONDS) -> None:
        self.primary = primary
        self.mirror = mirror
        self._retry_after = retry_after_seconds
        # 0.0 = primary believed healthy; otherwise the monotonic time before
        # which we won't touch it again.
        self._offline_until = 0.0
        self._pending: deque[dict] = deque()
        self._dropped_writes = 0
        self._lock = threading.Lock()

    @property
    def database_name(self) -> str:
        """The MongoDB database writes actually land in — surfaced so the UI can
        confirm it, since a database named in the URI overrides the setting."""
        return getattr(self.primary, "database_name", "")

    @property
    def primary_offline(self) -> bool:
        """Is MongoDB currently being skipped? True between a failure and the
        next retry — reads and writes are being served locally meanwhile."""
        with self._lock:
            return self._offline_until != 0.0 and time.monotonic() < self._offline_until

    @property
    def pending_write_count(self) -> int:
        """Messages saved locally but not yet in MongoDB (replayed on recovery)."""
        with self._lock:
            return len(self._pending)

    # --- primary health -------------------------------------------------

    def _primary_ready(self) -> bool:
        with self._lock:
            return self._offline_until == 0.0 or time.monotonic() >= self._offline_until

    def _note_primary_failure(self, action: str, ex: Exception) -> None:
        with self._lock:
            first_failure = self._offline_until == 0.0
            self._offline_until = time.monotonic() + self._retry_after
        if first_failure:
            # Message only, no traceback: a network timeout's stack is the same
            # 30 frames of pymongo internals every time and buries the log.
            logger.warning(
                "MongoDB unreachable while %s (%s) — using the local copy, retrying in %.0fs",
                action, ex, self._retry_after)
        else:
            logger.debug("MongoDB still unreachable while %s", action)

    def _note_primary_recovered(self) -> None:
        with self._lock:
            if self._offline_until == 0.0:
                return
            self._offline_until = 0.0
            dropped = self._dropped_writes
            self._dropped_writes = 0
        if dropped:
            logger.warning(
                "MongoDB is reachable again — %d message(s) held during the outage were dropped "
                "and exist only in local storage", dropped)
        else:
            logger.info("MongoDB is reachable again")

    def _hold_for_primary(self, record: dict) -> None:
        with self._lock:
            if len(self._pending) >= MAX_PENDING_PRIMARY_WRITES:
                self._pending.popleft()
                self._dropped_writes += 1
            self._pending.append(record)

    def _flush_pending(self) -> None:
        """Replay writes made while MongoDB was away. Stops at the first failure
        and leaves the rest queued, so ordering is preserved."""
        while True:
            with self._lock:
                if not self._pending:
                    return
                record = self._pending[0]
            self.primary.append_chat_memory(**record)   # raises -> caller marks offline
            with self._lock:
                if self._pending and self._pending[0] is record:
                    self._pending.popleft()

    # --- the store surface ----------------------------------------------

    def append_chat_memory(self, group_name: str, role: str, sender: str, text: str, keep: int = 24,
                           *, media_kind: str = "", media_note: str = "", media_path: str = "",
                           time_text: str = "") -> None:
        record = {
            "group_name": group_name, "role": role, "sender": sender, "text": text, "keep": keep,
            "media_kind": media_kind, "media_note": media_note, "media_path": media_path,
            "time_text": time_text,
        }
        # The local mirror first and always: it's the copy that must never miss
        # a message, and it can't be blocked by a network problem.
        try:
            self.mirror.append_chat_memory(**record)
        except Exception:  # noqa: BLE001
            logger.warning("chat memory append to the local store failed", exc_info=True)

        if not self._primary_ready():
            self._hold_for_primary(record)
            return
        try:
            self._flush_pending()
            self.primary.append_chat_memory(**record)
            self._note_primary_recovered()
        except Exception as ex:  # noqa: BLE001
            self._note_primary_failure("saving a message", ex)
            self._hold_for_primary(record)

    def get_chat_memory(self, group_name: str, limit: int = 24) -> list[tuple[str, str, str]]:
        if self._primary_ready():
            try:
                self._flush_pending()
                rows = self.primary.get_chat_memory(group_name, limit)
                self._note_primary_recovered()
                return rows
            except Exception as ex:  # noqa: BLE001 - MongoDB away: serve the local copy
                self._note_primary_failure("reading chat memory", ex)
        return self.mirror.get_chat_memory(group_name, limit)

    def get_chat_memory_rich(self, group_name: str, limit: int = 24) -> list[dict]:
        if self._primary_ready():
            try:
                self._flush_pending()
                rows = self.primary.get_chat_memory_rich(group_name, limit)
                self._note_primary_recovered()
                return rows
            except Exception as ex:  # noqa: BLE001
                self._note_primary_failure("reading chat memory", ex)
        return self.mirror.get_chat_memory_rich(group_name, limit)

    def clear_chat_memory(self, group_name: str) -> None:
        # Not replayed if MongoDB is away: nothing in the app clears memory
        # (it's persistent by design), so a missed clear can't strand the user.
        if self._primary_ready():
            try:
                self.primary.clear_chat_memory(group_name)
                self._note_primary_recovered()
            except Exception as ex:  # noqa: BLE001
                self._note_primary_failure("clearing chat memory", ex)
        try:
            self.mirror.clear_chat_memory(group_name)
        except Exception:  # noqa: BLE001
            logger.warning("clearing chat memory in the local store failed", exc_info=True)

    def get_chats_with_memory(self) -> list[tuple[str, int]]:
        if self._primary_ready():
            try:
                self._flush_pending()
                chats = self.primary.get_chats_with_memory()
                self._note_primary_recovered()
                return chats
            except Exception as ex:  # noqa: BLE001
                self._note_primary_failure("listing chats", ex)
        return self.mirror.get_chats_with_memory()

    def reconcile(self) -> int:
        """Make both stores agree. A chat present in only one is copied to the
        other; a chat in BOTH has any messages MongoDB is missing carried up
        into it. Returns how many messages were copied INTO the primary — i.e.
        carried up from local into MongoDB — for the "moved N over"
        confirmation.

        Healing inside an existing chat is what closes the one hole through
        which a relayed message could never reach MongoDB at all: writes made
        while the primary was unreachable are held in `_pending`, which is
        in-memory, so closing the app during an outage loses them. The local
        mirror still has every one, but reconcile used to SKIP any chat MongoDB
        already had — so the gap was permanent, and since reads prefer the
        primary, that message was invisible to the AI's memory afterwards.

        Deliberately one-directional: the upward copy heals, the downward one
        does not. Appending into the local mirror trims that chat to `keep`, so
        healing downward would cut a 400-message local archive to 24. MongoDB
        is unbounded and ignores `keep`, which is why upward is safe."""
        into_primary = copy_chat_memory(self.mirror, self.primary, heal_existing=True)
        copy_chat_memory(self.primary, self.mirror)
        return into_primary

    def close(self) -> None:
        for store in (self.primary, self.mirror):
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()


def _message_identity(row: dict) -> tuple:
    """What makes two remembered messages the same message.

    Everything the store persists about it EXCEPT when it was copied — the two
    stores write their own `created_utc`/`seq`, so those differ for the same
    logical message and cannot be part of its identity."""
    return (
        row.get("role", ""), row.get("sender", ""), row.get("text", ""),
        row.get("media_kind", ""), row.get("media_note", ""),
        row.get("media_path", ""), row.get("time_text", ""),
    )


def copy_chat_memory(src: ChatMemoryStore, dst: ChatMemoryStore, keep: int = 24,
                     *, heal_existing: bool = False) -> int:
    """Copy every chat's remembered messages from `src` into `dst`, so nothing
    is left behind when the user switches storage backends. Returns the number
    of messages copied. Never raises — a copy failure just means fewer messages
    migrated, not a broken switch.

    A chat `dst` already has is by default SKIPPED, so reconnecting doesn't
    duplicate. With `heal_existing`, such a chat is instead compared message by
    message and only what `dst` is missing is appended — the difference between
    "don't duplicate" and "don't lose anything", which matters when `dst` missed
    a few messages of a chat it otherwise has (see MirroredChatMemoryStore.
    reconcile for how that happens, and why only the upward copy asks for it).

    Counted, not set-compared: a chat legitimately contains the same text twice
    ("ok", "ok"), and a set difference would treat the second as already
    present and silently drop it."""
    copied = 0
    try:
        already = {group for group, _ in dst.get_chats_with_memory()}
        for group, _count in src.get_chats_with_memory():
            rows = src.get_chat_memory_rich(group, limit=100_000)
            if group in already:
                if not heal_existing:
                    continue
                # Only the messages dst hasn't got. Appended in src order, and
                # since what goes missing is almost always the most RECENT
                # traffic (written while the primary was away), appending puts
                # them back in the right place chronologically.
                have = Counter(
                    _message_identity(r) for r in dst.get_chat_memory_rich(group, limit=100_000)
                )
                missing = []
                for row in rows:
                    key = _message_identity(row)
                    if have[key] > 0:
                        have[key] -= 1
                    else:
                        missing.append(row)
                rows = missing
            for row in rows:
                dst.append_chat_memory(
                    group, row.get("role", ""), row.get("sender", ""), row.get("text", ""), keep=keep,
                    media_kind=row.get("media_kind", ""), media_note=row.get("media_note", ""),
                    media_path=row.get("media_path", ""), time_text=row.get("time_text", ""),
                )
                copied += 1
    except Exception:  # noqa: BLE001
        logger.warning("copying chat memory between stores failed partway", exc_info=True)
    return copied


def try_build_chat_memory_store(uri: str, sqlite_fallback: ChatMemoryStore,
                                database: str = "") -> tuple[ChatMemoryStore, str]:
    """Build the store AND say why, in plain English, if MongoDB was skipped.

    Returns `(store, problem)`: `problem` is empty on success (or when no URI
    is configured at all, which is not a problem — it's the default). Never
    raises; any Mongo trouble falls back to `sqlite_fallback` so chat memory
    keeps working. The reason is returned rather than only logged so the
    Settings panel can tell the user what to fix instead of a bare
    "couldn't connect"."""
    uri = (uri or "").strip()
    if not uri:
        return sqlite_fallback, ""
    try:
        mongo = MongoChatMemoryStore(uri, database=database)
    except Exception as ex:  # noqa: BLE001 - pymongo missing, unreachable, auth, etc.
        problem = describe_connection_problem(ex, uri)
        logger.warning("MongoDB chat memory unavailable (%s) — using local storage instead", problem)
        return sqlite_fallback, problem
    return MirroredChatMemoryStore(primary=mongo, mirror=sqlite_fallback), ""


def build_chat_memory_store(uri: str, sqlite_fallback: ChatMemoryStore,
                            database: str = "") -> ChatMemoryStore:
    """When `uri` is set and the server answers, return a store that MIRRORS
    to both MongoDB and the local SQLite `sqlite_fallback`, so the two never
    diverge; otherwise return the SQLite fallback alone. Never raises: any
    Mongo problem logs a warning and falls back, so chat memory always works
    even if MongoDB is down."""
    return try_build_chat_memory_store(uri, sqlite_fallback, database)[0]
