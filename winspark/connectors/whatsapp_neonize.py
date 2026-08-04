"""SKETCH — a WhatsApp sender/reader that speaks the multi-device protocol
instead of driving WhatsApp Desktop's window.

Nothing in the app uses this yet. It exists to be tried against a spare number:
it implements the same small surface `WhatsAppGroupSender` exposes, so the relay
service, "!name" command mode, chat memory and the UI work against it unchanged.

Why bother, given the UI-Automation sender works:

- **Media.** `whatsapp.py` can only screenshot the on-screen thumbnail, because
  the accessibility tree never exposes a file — voice notes and full-resolution
  photos are simply unobtainable that way. Here every attachment arrives as real
  bytes and lands on disk, which is the only reason this sketch exists.
- **Real message IDs.** `_message_identities` in the relay reconstructs an
  identity from (timestamp, occurrence, text) purely because the accessibility
  tree has no ID to key on. The protocol gives one.
- **Push, not poll.** Messages arrive on an event; no 3-second poll that opens
  the chat, steals the foreground and drives the real keyboard.

What it costs, and these are not small:

- **Ban risk.** This is a reverse-engineered client, not Meta's official API.
  neonize/whatsmeow's own guidance is to never pair a primary personal number.
  The UI-Automation sender drives the *real* WhatsApp client and is far safer on
  that axis; it should stay the default.
- **No history.** whatsmeow cannot ask WhatsApp for a chat's past messages — a
  freshly paired session sees only what arrives from now on. So the recent-window
  read below is served from an in-process buffer this class fills as messages
  come in, NOT a query. On a cold start the buffer is empty, where the
  UI-Automation reader would have seen whatever is on screen.

Unverified: written against neonize's published API without a paired session to
run it. The two things to check first against your installed version are the
joined-groups call in `_load_directory` and the protobuf field access in
`_media_of` — both are isolated so a mismatch is a small edit.

Install:  pip install neonize        (Python >=3.10; Windows wheels published)
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import threading
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from winspark.connectors.fetch_webhook_models import WhatsAppGroupSendResult
from winspark.connectors.whatsapp import (
    MEDIA_DOCUMENT,
    MEDIA_GIF,
    MEDIA_PHOTO,
    MEDIA_STICKER,
    MEDIA_VIDEO,
    MEDIA_VOICE,
    WhatsAppMessage,
    media_placeholder,
)
from winspark.connectors.whatsapp_chat_name_rules import chat_names_match

logger = logging.getLogger(__name__)

# How many incoming messages to keep per chat. The relay's backlog walk asks for
# ~10; this leaves room for a burst between polls without growing unbounded on a
# busy group.
RECENT_PER_CHAT = 50


class NeonizeUnavailableError(RuntimeError):
    """Raised when the optional `neonize` package isn't installed."""


def _require_neonize():
    try:
        from neonize.client import NewClient
        from neonize.events import ConnectedEv, MessageEv
        from neonize.utils import build_jid
    except ImportError as ex:  # pragma: no cover - optional dependency
        raise NeonizeUnavailableError(
            "the 'neonize' package is required for the protocol-backed WhatsApp "
            "connector — run: pip install neonize"
        ) from ex
    return NewClient, ConnectedEv, MessageEv, build_jid


def _media_of(proto_message) -> tuple[str, str, object]:
    """(media_kind, media_note, the sub-message carrying the bytes).

    Maps WhatsApp's protobuf message types onto the MEDIA_* vocabulary the rest
    of winSpark already speaks, so chat memory and the AI prompt see exactly what
    they see from the UI-Automation reader. Returns ("", "", None) for plain text.

    A GIF is a videoMessage with `gifPlayback` set, not its own type — checked
    before video so it isn't mislabelled."""
    m = proto_message
    if m.HasField("imageMessage"):
        return MEDIA_PHOTO, m.imageMessage.caption or "", m.imageMessage
    if m.HasField("audioMessage"):
        seconds = int(getattr(m.audioMessage, "seconds", 0) or 0)
        note = f"{seconds // 60}:{seconds % 60:02d}" if seconds else ""
        return MEDIA_VOICE, note, m.audioMessage
    if m.HasField("videoMessage"):
        kind = MEDIA_GIF if getattr(m.videoMessage, "gifPlayback", False) else MEDIA_VIDEO
        return kind, m.videoMessage.caption or "", m.videoMessage
    if m.HasField("documentMessage"):
        return MEDIA_DOCUMENT, m.documentMessage.fileName or "", m.documentMessage
    if m.HasField("stickerMessage"):
        return MEDIA_STICKER, "", m.stickerMessage
    return "", "", None


def _plain_text(proto_message) -> str:
    """The text of a text message, across the two shapes WhatsApp uses."""
    if proto_message.conversation:
        return proto_message.conversation
    extended = proto_message.extendedTextMessage
    return extended.text if extended and extended.text else ""


def _jid_str(jid) -> str:
    """"919999999999@s.whatsapp.net" from a JID.

    NOT str(jid): a JID is a protobuf message, so str() renders its debug text
    ('User: "9199..."\\nServer: "s.whatsapp.net"\\n'), which is useless as a key
    and silently wrong as a dict lookup."""
    user = getattr(jid, "User", "") or ""
    server = getattr(jid, "Server", "") or "s.whatsapp.net"
    return f"{user}@{server}" if user else ""


def _time_label(unix_seconds: int) -> str:
    """"9:21 pm" — the same shape the UI-Automation reader lifts off the bubble,
    so a chat's memory looks identical whichever connector recorded it.

    MessageInfo.Timestamp is an int64 unix time, not a datetime, and the hour is
    un-padded WITHOUT %-I/%#I: those are non-portable and %-I raises ValueError
    on Windows, which is the only platform winSpark runs on."""
    if not unix_seconds:
        return ""
    try:
        stamp = datetime.fromtimestamp(int(unix_seconds))
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.strftime("%I:%M %p").lstrip("0").lower()


class NeonizeGroupSender:
    """Drop-in alternative to `WhatsAppGroupSender`.

    Implements the surface the rest of the app actually calls:
      send_to_group_async / read_recent_incoming_async / read_last_incoming_async
      / read_last_incoming_message_async / resolve_chat_name_async
      / can_resolve_chat_async / open_chat_async
    """

    def __init__(self, session_name: str, database_path: Path, media_dir: Path) -> None:
        self._session_name = session_name
        self._database_path = Path(database_path)
        self._media_dir = Path(media_dir)
        self._client = None
        self._connected = threading.Event()
        # chat JID -> newest-last incoming messages, filled by the event handler.
        # whatsmeow can't fetch history, so this buffer IS the recent window.
        self._recent: dict[str, deque[WhatsAppMessage]] = defaultdict(
            lambda: deque(maxlen=RECENT_PER_CHAT))
        # display name -> JID, for turning "Nagen US" into a routable address.
        self._directory: dict[str, str] = {}
        self._lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Pair (QR on first run) and begin receiving. Blocks until connected."""
        NewClient, ConnectedEv, MessageEv, _build_jid = _require_neonize()
        self._media_dir.mkdir(parents=True, exist_ok=True)
        client = NewClient(name=self._session_name, database=str(self._database_path))

        @client.event(ConnectedEv)
        def _on_connected(_client, _event):  # noqa: ANN001
            logger.info("WhatsApp (neonize) session connected")
            self._load_directory()
            self._connected.set()

        @client.event(MessageEv)
        def _on_message(_client, event):  # noqa: ANN001
            try:
                self._ingest(event)
            except Exception:  # noqa: BLE001 - one bad message must not kill the listener
                logger.warning("failed to ingest an incoming WhatsApp message", exc_info=True)

        self._client = client
        threading.Thread(target=client.connect, name="neonize", daemon=True).start()
        self._connected.wait(timeout=60)

    def _load_directory(self) -> None:
        """Build the display-name -> JID map.

        GROUPS ONLY, and that is not an oversight: neonize exposes no
        list-my-contacts call at all (there is `get_user_info(*jids)`, but it
        answers about JIDs you already hold). So a one-to-one chat cannot be
        looked up by name up front — it is learned from `Pushname` when someone
        messages you (see `_remember_sender`), or addressed by phone number.

        This is a real regression against the UI-Automation reader, which can
        see every chat in the sidebar. It mostly doesn't bite the "!name" bot,
        which only ever replies to chats that just spoke — but "send to Mum"
        before Mum has ever messaged you will fail to resolve."""
        directory: dict[str, str] = {}
        try:
            for group in self._client.get_joined_groups():
                name = (getattr(group, "GroupName", "") or "").strip()
                jid = _jid_str(getattr(group, "JID", None))
                if name and jid:
                    directory[name] = jid
        except Exception:  # noqa: BLE001
            logger.warning("could not read the joined WhatsApp groups", exc_info=True)

        with self._lock:
            self._directory.update(directory)
            total = len(self._directory)
        logger.info("WhatsApp directory: %d group(s) loaded, %d chat(s) known", len(directory), total)

    def _remember_sender(self, chat_jid: str, push_name: str, is_group: bool) -> None:
        """Learn a one-to-one chat's display name from whoever just messaged.
        The only way this connector ever knows a personal chat by name."""
        name = (push_name or "").strip()
        if name and not is_group:
            with self._lock:
                self._directory.setdefault(name, chat_jid)

    # --- receiving ------------------------------------------------------

    def _ingest(self, event) -> None:
        """Turn one incoming protobuf message into a WhatsAppMessage, saving any
        attachment to disk, and buffer it under its chat."""
        info = event.Info
        if info.MessageSource.IsFromMe:
            return  # our own messages are not something to reply to

        chat_jid = _jid_str(info.MessageSource.Chat)
        if not chat_jid:
            return
        sender = (getattr(info, "Pushname", "") or "").strip() or info.MessageSource.Sender.User
        self._remember_sender(chat_jid, getattr(info, "Pushname", ""),
                              bool(info.MessageSource.IsGroup))
        media_kind, media_note, _payload = _media_of(event.Message)

        media_path = ""
        text = _plain_text(event.Message)
        if media_kind:
            media_path = self._save_media(event, info, media_kind)
            # The same readable stand-in the UI reader produces, so prompts and
            # the memory view render identically whichever connector is in use.
            text = media_placeholder(media_kind, media_note)

        if not text.strip():
            return

        message = WhatsAppMessage(
            sender=sender,
            text=text,
            is_incoming=True,
            media_kind=media_kind,
            media_note=media_note,
            time_text=_time_label(info.Timestamp),
            media_path=media_path,
        )
        with self._lock:
            self._recent[chat_jid].append(message)

    def _save_media(self, event, info, media_kind: str) -> str:
        """Download the attachment's real bytes to disk, returning the path.

        This is the whole point of this connector: `download_any` hands back the
        actual file — a full-resolution photo, the voice note itself — where the
        UI-Automation path can only screenshot a thumbnail and can't reach a
        voice note at all. Failure returns "" so the message is still recorded
        with its placeholder text."""
        try:
            suffix = mimetypes.guess_extension(
                getattr(_media_of(event.Message)[2], "mimetype", "") or "") or ".bin"
            target = self._media_dir / f"{info.ID}_{media_kind}{suffix}"
            self._client.download_any(event.Message, str(target))
            return str(target) if target.exists() else ""
        except Exception:  # noqa: BLE001 - a missing attachment must not lose the message
            logger.warning("could not download a %s attachment", media_kind, exc_info=True)
            return ""

    # --- the surface the app calls --------------------------------------

    def _jid_for(self, group_name: str) -> Optional[str]:
        """Resolve a chat's display name to a JID, reusing the same fuzzy name
        matching the UI-Automation sender uses so both behave alike."""
        wanted = (group_name or "").strip()
        if not wanted:
            return None
        with self._lock:
            directory = dict(self._directory)
        if wanted in directory:
            return directory[wanted]
        for name, jid in directory.items():
            if chat_names_match(name, wanted):
                return jid
        # A bare phone number addresses a one-to-one chat directly — the only
        # way to reach someone who has never messaged you, given there's no
        # contact list to search (see _load_directory).
        digits = "".join(ch for ch in wanted if ch.isdigit())
        if digits and digits == wanted.lstrip("+").replace(" ", "").replace("-", ""):
            return f"{digits}@s.whatsapp.net"
        return None

    async def send_to_group_async(self, group_name: str, message_text: str) -> WhatsAppGroupSendResult:
        jid_str = self._jid_for(group_name)
        if jid_str is None:
            return WhatsAppGroupSendResult.failed(f"Chat '{group_name}' not found.")
        try:
            _NewClient, _C, _M, build_jid = _require_neonize()
            user, _, server = jid_str.partition("@")
            # Sending is one call. No foreground window, no compose box to verify,
            # no clipboard, no 30ms-per-character typing — the entire class of
            # send bugs in whatsapp_group_sender.py does not exist here.
            await asyncio.to_thread(
                self._client.send_message, build_jid(user, server or "s.whatsapp.net"), message_text)
        except Exception as ex:  # noqa: BLE001
            return WhatsAppGroupSendResult.failed(f"Could not send: {ex}")
        return WhatsAppGroupSendResult.succeeded("sent", verified=True, appeared=True)

    async def read_recent_incoming_async(self, group_name: str, limit: int = 10) -> list[WhatsAppMessage]:
        """Oldest-first recent incoming messages, from the buffer this class
        fills as they arrive. NOT a query: whatsmeow cannot fetch history, so a
        chat is empty here until something is said while the session is up."""
        jid_str = self._jid_for(group_name)
        if jid_str is None:
            return []
        with self._lock:
            return list(self._recent.get(jid_str, ()))[-max(1, limit):]

    async def read_last_incoming_async(self, group_name: str) -> Optional[WhatsAppMessage]:
        recent = await self.read_recent_incoming_async(group_name, limit=1)
        return recent[-1] if recent else None

    async def read_last_incoming_message_async(self, group_name: str) -> Optional[str]:
        message = await self.read_last_incoming_async(group_name)
        return message.text if message is not None else None

    async def resolve_chat_name_async(self, chat_name: str) -> str:
        jid_str = self._jid_for(chat_name)
        if jid_str is None:
            return ""
        with self._lock:
            for name, jid in self._directory.items():
                if jid == jid_str:
                    return name
        return chat_name

    async def can_resolve_chat_async(self, chat_name: str) -> bool:
        return self._jid_for(chat_name) is not None

    async def open_chat_async(self, group_name: str) -> bool:
        """No-op: there is no window to open. True when the chat is reachable,
        so the UI's "Open chat" check still means something."""
        return self._jid_for(group_name) is not None

    def close(self) -> None:
        disconnect = getattr(self._client, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:  # noqa: BLE001
                pass
