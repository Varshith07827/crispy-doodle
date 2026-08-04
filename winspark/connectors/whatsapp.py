"""WhatsApp Desktop connector — reads the chat list, unread count, and active
conversation via UI Automation only. No OCR, no screenshots.

This is NOT a port of WinSpark.Infrastructure.Connectors.WhatsApp (the real
.NET connector is ~39 files built around OCR + visual detection as a
fallback for reading chat/message content). This was built fresh after
discovering, by inspecting a live WhatsApp Desktop window, that its "Chat
list" control is a virtualized DataGrid: a plain UI Automation tree walk
(GetChildren()) sees zero rows, but `GridPattern.GetItem(row, 0)` returns rows
with contact name, last message preview, timestamp, and unread count all
present in the row's accessible Name — no screenshot or OCR confidence score
needed. That's a real capability gap the .NET codebase doesn't appear to use.

Caveat, found by actually running this against a 511-chat list rather than
assuming: GridPattern.RowCount reports the list's full logical row count, but
GetItem() only succeeds for rows Chromium has realized in the accessibility
tree near the current scroll position (unlike native virtualized UIA
controls, which page in whatever row you ask for). Past that range it throws
COMError. So read_chat_rows_async returns whatever's currently realized —
in practice the chats visible plus a nearby buffer, not the entire history —
and would need programmatic scrolling to walk further down the list.

The row text has no field delimiters (Chromium flattens all descendant text
into one accessible Name string), so parsing it is a tuned heuristic — see
whatsapp_row_parser.py, tested against 5 real rows captured live.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from winspark.automation.sta_thread_manager import StaAutomationThreadManager
from winspark.connectors.models import WhatsAppChatRow
from winspark.connectors.whatsapp_row_parser import parse_chat_row

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    import win32gui
    import win32process

    _WIN32_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _WIN32_AVAILABLE = False

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

_WHATSAPP_PROCESS_NAMES = {"whatsapp.exe", "whatsapp.root.exe"}


try:
    from _ctypes import COMError
except ImportError:  # pragma: no cover - off-Windows
    class COMError(Exception):  # type: ignore[no-redef]
        pass


def _absorb_com_errors(default):
    """WhatsApp re-renders its React tree whenever the user switches chats or a
    message arrives, killing UIA elements mid-search — reading a property then
    raises COMError ("An event was unable to invoke any of the subscribers",
    seen live during the 3s poll, escaping uiautomation's own Exists()). These
    are transient: the very next poll succeeds. So every sync read absorbs
    COMError and returns its benign default instead of erroring a whole cycle."""
    def wrap(func):
        import functools

        @functools.wraps(func)
        def guarded(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except COMError:
                logger.debug("transient UIA COMError in %s — returning default", func.__name__)
                return default() if callable(default) else default
        return guarded
    return wrap


_SELF_SENDER_LABEL = "You:"
_MESSAGE_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[ap]m\b", re.IGNORECASE)
# A full time LABEL ("9:21 pm"), used to lift a bubble's own timestamp out of
# its text controls. Kept distinct from _MESSAGE_TIME_RE (a prefix test used to
# DROP the timestamp from message text).
_TIME_LABEL_RE = re.compile(r"^\d{1,2}:\d{2}\s*[ap]m$", re.IGNORECASE)
# A bare voice/video duration ("0:12", "1:03") — no am/pm, distinguishing it
# from a clock time.
_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")

# Attachment kinds the accessibility tree can name but not hand us the bytes of.
MEDIA_PHOTO = "photo"
MEDIA_VOICE = "voice"
MEDIA_VIDEO = "video"
MEDIA_DOCUMENT = "document"
MEDIA_STICKER = "sticker"
MEDIA_GIF = "gif"

# Signal words in a bubble's control names (buttons/text) that identify each
# attachment kind. Tuned to WhatsApp Desktop's accessibility labels; heuristic
# like the rest of this connector, and structured so they're easy to extend as
# live labels are observed. Order matters: the first kind whose signal is
# present wins, so more specific kinds (voice/video) precede the generic photo.
_MEDIA_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MEDIA_VOICE, ("voice message", "play voice", "voice note")),
    (MEDIA_VIDEO, ("play video", "video message")),
    (MEDIA_GIF, ("gif",)),
    (MEDIA_STICKER, ("sticker",)),
    (MEDIA_DOCUMENT, ("download", "document", "open document")),
    (MEDIA_PHOTO, ("open photo", "photo", "image", "view photo")),
)

_MEDIA_PLACEHOLDERS = {
    MEDIA_PHOTO: "Photo",
    MEDIA_VOICE: "Voice note",
    MEDIA_VIDEO: "Video",
    MEDIA_DOCUMENT: "Document",
    MEDIA_STICKER: "Sticker",
    MEDIA_GIF: "GIF",
}

# A filename-looking token ("report.pdf", "IMG_2043.jpg") used to pull a
# document's name out of its bubble for the note.
_FILENAME_RE = re.compile(r"^[\w .()\-]+\.[A-Za-z0-9]{1,8}$")

# WhatsApp renders chrome INTO the message list that nobody actually sent: the
# encryption banner, the date dividers, "You deleted this message". They read
# back exactly like messages, so they were being stored as chat memory — found
# live with the e2e banner filed under role="me" — and then fed to the AI as
# conversation context and searched by RAG.
#
# Named _CHROME_* rather than _SYSTEM_NOTICE_*: this module already has a
# _SYSTEM_NOTICE_PREFIXES further down (the view-once notice, used by the
# group-bubble reader). Reusing that name silently redefined it — the later
# binding won and this filter did nothing, which is how a test caught it.
#
# Substrings, for the long notices whose wording varies by chat type
# ("Messages and calls are…" vs "Messages to yourself are…").
_CHROME_SUBSTRINGS = (
    "end-to-end encrypted",
    "click to learn more",
    "tap to learn more",
    "this business works with other companies",
)
# Prefixes, for the group-admin notices that carry a variable tail.
_CHROME_PREFIXES = (
    "you changed the group name",
    "you changed this group's icon",
    "you created group",
    "your security code with",
    "messages and calls are",
    "messages to yourself are",
)
# Whole-line matches only — a date divider reads as the bare word "Today", but
# "Today I went out" is a real message, so these must never match as substrings.
_CHROME_EXACT = frozenset({
    "today", "yesterday",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "you deleted this message", "this message was deleted",
})


def is_system_notice(text: str) -> bool:
    """Is this WhatsApp's own UI chrome rather than something a person sent?"""
    value = (text or "").strip().lower().rstrip(".")
    if not value:
        return False
    if value in _CHROME_EXACT:
        return True
    if any(value.startswith(p) for p in _CHROME_PREFIXES):
        return True
    return any(s in value for s in _CHROME_SUBSTRINGS)


def media_placeholder(media_kind: str, media_note: str = "") -> str:
    """The readable text stand-in stored in a media message's ``text`` field,
    e.g. ``"[Voice note · 0:12]"`` or ``"[Document: report.pdf]"``. A caption is
    appended after the marker so ``"[Photo] look at this"`` reads naturally."""
    label = _MEDIA_PLACEHOLDERS.get(media_kind)
    if not label:
        return ""
    note = (media_note or "").strip()
    if media_kind == MEDIA_VOICE and note:
        return f"[{label} · {note}]"
    if media_kind == MEDIA_DOCUMENT and note:
        return f"[{label}: {note}]"
    if media_kind == MEDIA_PHOTO and note:
        # A photo caption (or, later, a saved-thumbnail marker) reads after the tag.
        return f"[{label}] {note}"
    return f"[{label}]"


@dataclass(frozen=True)
class WhatsAppMessage:
    """One message bubble read from the open conversation.

    ``media_kind``/``media_note`` describe an attachment the accessibility tree
    can only *name*, never hand us the bytes of: a photo, voice note, video,
    document, sticker or GIF. ``media_kind`` is one of the ``MEDIA_*`` constants
    (``""`` for a plain text message); ``media_note`` carries whatever detail the
    tree exposed — a voice note's duration, a document's filename, a photo's
    caption. ``text`` already contains a readable placeholder for media so every
    existing reader (AI prompt, memory viewer) works unchanged.

    ``time_text`` is the message's own timestamp label as WhatsApp renders it
    (e.g. ``"9:21 pm"``), or ``""`` when the bubble didn't expose one — kept
    separate from the insertion time so memory has the *message's* moment."""

    sender: str
    text: str
    is_incoming: bool  # True = from the other party, False = sent by us ("You:")
    media_kind: str = ""   # one of MEDIA_* below, or "" for a text-only message
    media_note: str = ""   # duration / filename / caption
    time_text: str = ""    # the bubble's own time label, e.g. "9:21 pm"
    # Screen-coordinate rect of the bubble's picture (photo/video/sticker/gif),
    # or None. The one hook the optional thumbnail-capture needs — pixels the
    # accessibility tree can't give us — without the reader itself touching disk.
    media_rect: Optional[tuple[int, int, int, int]] = None


class WhatsAppUnavailableError(RuntimeError):
    """Raised when pywin32/uiautomation isn't available (i.e. not on Windows)."""


class WhatsAppConnector:
    def __init__(self, sta_manager: StaAutomationThreadManager) -> None:
        self._sta_manager = sta_manager

    async def find_window_async(self) -> Optional[int]:
        return await self._sta_manager.invoke_async(_find_window_sync)

    async def get_unread_badge_count_async(self, window_handle: int) -> int:
        return await self._sta_manager.invoke_async(lambda: _get_unread_badge_count_sync(window_handle))

    async def read_chat_rows_async(self, window_handle: int) -> list[WhatsAppChatRow]:
        return await self._sta_manager.invoke_async(lambda: _read_chat_rows_sync(window_handle))

    async def read_chat_rows_deep_async(self, window_handle: int, max_scrolls: int = 6) -> list[WhatsAppChatRow]:
        """Scroll the chat list to read beyond the realized window — the full
        (or much deeper) recents list rather than just what's on screen."""
        return await self._sta_manager.invoke_async(
            lambda: _read_chat_rows_deep_sync(window_handle, max_scrolls)
        )

    async def get_unread_chats_async(self, window_handle: int) -> list[WhatsAppChatRow]:
        rows = await self.read_chat_rows_async(window_handle)
        return [r for r in rows if r.unread_count > 0]

    async def get_active_conversation_name_async(self, window_handle: int) -> Optional[str]:
        return await self._sta_manager.invoke_async(lambda: _get_active_conversation_name_sync(window_handle))

    async def read_last_message_async(self, window_handle: int) -> Optional[WhatsAppMessage]:
        return await self._sta_manager.invoke_async(lambda: _read_last_message_sync(window_handle))

    async def read_recent_messages_async(self, window_handle: int, limit: int = 20) -> list[WhatsAppMessage]:
        return await self._sta_manager.invoke_async(lambda: _read_recent_messages_sync(window_handle, limit))

    async def read_open_conversation_async(self, window_handle: int, limit: int = 20):
        """(active_conversation_name, recent_messages) for whatever chat is open,
        read in one STA round-trip. No opening or foregrounding — a cheap read."""
        return await self._sta_manager.invoke_async(lambda: _read_open_conversation_sync(window_handle, limit))


def _require_win32() -> None:
    if not _WIN32_AVAILABLE:
        raise WhatsAppUnavailableError("pywin32 + uiautomation are required and only available on Windows")


def _find_window_sync() -> Optional[int]:
    _require_win32()
    found: list[int] = []

    def _callback(hwnd: int, _: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if psutil is None:
            return True
        try:
            name = psutil.Process(pid).name()
        except psutil.NoSuchProcess:
            return True
        if name.lower() in _WHATSAPP_PROCESS_NAMES and win32gui.GetWindowText(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_callback, None)
    return found[0] if found else None


@_absorb_com_errors(0)
def _get_unread_badge_count_sync(window_handle: int) -> int:
    _require_win32()
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return 0

    tab = auto.Control(
        searchFromControl=root, searchDepth=40, ControlType=auto.ControlType.TabItemControl, RegexName=r"^Unread\b.*"
    )
    if not tab.Exists(1, 0.2):
        return 0

    match = re.fullmatch(r"Unread\s*(\d+)?", (tab.Name or "").strip())
    return int(match.group(1)) if match and match.group(1) else 0


# Section labels WhatsApp injects into the "Search results." grid (they aren't
# chats). Matched case-insensitively; a real chat literally named one of these
# is vanishingly unlikely and would just be skipped from search results.
_SEARCH_SECTION_HEADERS = {
    "chats",
    "groups in common",
    "messages",
    "contacts",
    "contacts on whatsapp",
    "other contacts",
}


def _is_search_section_header(name: str) -> bool:
    return name.strip().lower() in _SEARCH_SECTION_HEADERS


def _iter_grid_row_controls(chat_list) -> list:
    """Return the row controls of a WhatsApp chat-list-style DataGrid.

    The two grids expose their rows in *opposite* ways, confirmed live:

    - "Chat list" (recents) is virtualized — a plain child walk (GetChildren)
      sees zero rows, but GridPattern.GetItem(row, 0) returns them (only for the
      rows Chromium has realized near the current scroll position; GetItem
      raises COMError past that range, which just ends the read).
    - "Search results." is the reverse — GridPattern.GetItem throws COMError for
      every row ("the server threw an exception"), but all rows are present as
      direct DataItem children readable via GetChildren().

    So try GetItem first (recents), and fall back to GetChildren (search)."""
    controls: list = []
    grid = chat_list.GetPattern(auto.PatternId.GridPattern)
    if grid is not None:
        try:
            row_count = grid.RowCount
        except Exception:  # noqa: BLE001
            row_count = 0
        for row_index in range(row_count):
            try:
                item = grid.GetItem(row_index, 0)
            except Exception:  # noqa: BLE001 - past realized range, or grid doesn't support GetItem
                break
            if item is not None:
                controls.append(item)
    if controls:
        return controls
    return [c for c in chat_list.GetChildren() if (c.Name or "").strip()]


def _find_chat_grid(window_handle: int, grid_name: str = "Chat list"):
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None
    chat_list = auto.Control(
        searchFromControl=root, searchDepth=40, Name=grid_name, ControlType=auto.ControlType.DataGridControl
    )
    return chat_list if chat_list.Exists(2, 0.3) else None


@_absorb_com_errors(list)
def _read_chat_rows_sync(window_handle: int, grid_name: str = "Chat list") -> list[WhatsAppChatRow]:
    """Read rows from a WhatsApp chat-list-style grid. `grid_name` is "Chat list"
    for the recents sidebar, or "Search results." while a search is active (both
    are DataGrids with the same per-row accessible-name format, but expose their
    rows via different UIA mechanisms — see _iter_grid_row_controls)."""
    _require_win32()
    chat_list = _find_chat_grid(window_handle, grid_name)
    if chat_list is None and grid_name == "Chat list":
        # An active search HIDES the recents grid entirely — recent WhatsApp
        # builds expose no chat DataGrid at all while a query is in the search
        # box (confirmed live: refresh returned 0 rows with a search typed).
        # Clear any active search (self-gated on the box having text) and
        # re-read, so refresh works no matter what's left in the search box.
        # Lazy import: group_sender imports this module at load time.
        from winspark.connectors.whatsapp_group_sender import _clear_search_sync

        _clear_search_sync(window_handle)
        time.sleep(0.4)  # let the recents grid re-render
        chat_list = _find_chat_grid(window_handle, grid_name)
    if chat_list is None:
        return []

    rows: list[WhatsAppChatRow] = []
    for item in _iter_grid_row_controls(chat_list):
        name = (item.Name or "").strip()
        if not name or _is_search_section_header(name):
            continue
        rows.append(WhatsAppChatRow(**parse_chat_row(name)))
    return rows


def _scroll_chat_list_sync(chat_list, down: bool = True) -> bool:
    """Scroll the chat-list grid one large step. Returns whether a scroll
    actually happened (False when the grid isn't scrollable or the pattern is
    unavailable), so the deep read knows when to stop."""
    try:
        scroll = chat_list.GetPattern(auto.PatternId.ScrollPattern)
    except Exception:  # noqa: BLE001
        return False
    if scroll is None:
        return False
    try:
        if not scroll.VerticallyScrollable:
            return False
        amount = auto.ScrollAmount.LargeIncrement if down else auto.ScrollAmount.LargeDecrement
        scroll.Scroll(auto.ScrollAmount.NoAmount, amount)
        return True
    except Exception:  # noqa: BLE001
        return False


def _scroll_chat_list_to_top_sync(chat_list) -> None:
    """Best-effort return the chat list to the top so a deep read doesn't leave
    the sidebar scrolled down under the user."""
    try:
        scroll = chat_list.GetPattern(auto.PatternId.ScrollPattern)
        if scroll is not None and scroll.VerticallyScrollable:
            scroll.SetScrollPercent(-1, 0)
    except Exception:  # noqa: BLE001
        pass


@_absorb_com_errors(list)
def _read_chat_rows_deep_sync(window_handle: int, max_scrolls: int = 6,
                              grid_name: str = "Chat list") -> list[WhatsAppChatRow]:
    """Like _read_chat_rows_sync but scrolls the list to reach chats below the
    initially-realized window, accumulating rows across scroll steps (deduped by
    chat name, first-seen order preserved). Stops early when a scroll yields no
    new chats or the grid can't scroll — so it reads as deep as the list goes
    without assuming a fixed length. Opt-in: the normal refresh stays shallow and
    fast; this is for "read my whole chat list".

    Not verified against a live 500-chat list end to end (the scroll calls need
    real WhatsApp); the accumulate/dedupe/stop logic is unit-tested against
    staged row batches."""
    _require_win32()
    chat_list = _find_chat_grid(window_handle, grid_name)
    if chat_list is None:
        return []

    by_name: dict[str, WhatsAppChatRow] = {}
    order: list[str] = []

    def ingest() -> None:
        for item in _iter_grid_row_controls(chat_list):
            name = (item.Name or "").strip()
            if not name or _is_search_section_header(name):
                continue
            row = WhatsAppChatRow(**parse_chat_row(name))
            if row.chat_name not in by_name:
                by_name[row.chat_name] = row
                order.append(row.chat_name)

    ingest()
    for _ in range(max(0, max_scrolls)):
        before = len(order)
        if not _scroll_chat_list_sync(chat_list, down=True):
            break
        time.sleep(0.35)  # let Chromium realize the next window of rows
        ingest()
        if len(order) == before:
            break  # nothing new surfaced — end of the list (or of what realizes)
    _scroll_chat_list_to_top_sync(chat_list)
    return [by_name[name] for name in order]


@_absorb_com_errors(None)
def _get_active_conversation_name_sync(window_handle: int) -> Optional[str]:
    _require_win32()
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None

    compose = auto.Control(
        searchFromControl=root, searchDepth=40, ControlType=auto.ControlType.EditControl, RegexName=r"^Type a message"
    )
    if not compose.Exists(1, 0.2):
        return None

    match = re.match(r"^Type a message to (.+)$", compose.Name or "")
    if not match:
        return None
    # Groups read as "Type a message to group <Name>"; show just the name.
    return re.sub(r"^group ", "", match.group(1))


# WhatsApp re-renders its React tree whenever the user switches chats or new
# messages arrive, which kills UIA elements we're holding mid-walk — reading a
# property on one then raises COMError ("An event was unable to invoke any of
# the subscribers", seen live during the 3s poll). Treat a dead element as
# empty/leafless so the read skips it and returns the rest, instead of the
# whole poll failing.
def _safe_name(ctrl) -> str:
    try:
        return ctrl.Name or ""
    except Exception:  # noqa: BLE001 - stale element
        return ""


def _safe_control_type(ctrl) -> str:
    try:
        return ctrl.ControlTypeName
    except Exception:  # noqa: BLE001 - stale element
        return ""


def _safe_children(ctrl) -> list:
    try:
        return ctrl.GetChildren()
    except Exception:  # noqa: BLE001 - stale element
        return []


@_absorb_com_errors(list)
def _read_recent_messages_sync(window_handle: int, limit: int = 20) -> list[WhatsAppMessage]:
    """Read the most recent message bubbles (up to `limit`) from the open
    conversation, oldest-first.

    Two shapes, both confirmed live:
    - One-to-one chats tag each bubble with a GroupControl named "You:" (sent by
      us) or "<Contact>:" (received) — see _read_labeled_messages.
    - Group chats have no such labels; each bubble is a DataItemControl row with
      the text in nested TextControls, and who-sent-it is only conveyed by
      left/right alignment — see _read_bubble_messages.

    Try the labelled shape first; if it finds nothing (a group, or a 1:1 with no
    realized labels), fall back to reading the bubble rows. Only bubbles Chromium
    has scrolled into view are realized, so this returns the visible tail rather
    than the full history."""
    _require_win32()
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return []

    messages = _read_labeled_messages(root)
    # In a GROUP chat only OUR bubbles carry the "You:" label group — other
    # members' messages have no label at all, so the labeled read sees just one
    # side of the conversation. Seen live: a group where members had sent
    # several messages read as nothing but our own, so "reply to the newest
    # incoming message" concluded there was never anything to reply to. When
    # the labeled view has no incoming messages, trust the bubble read if it
    # sees a fuller conversation.
    if not messages or not any(m.is_incoming for m in messages):
        bubbles = _read_bubble_messages(root)
        if len(bubbles) > len(messages):
            messages = bubbles
    # Drop WhatsApp's own notices here, at the one point BOTH read shapes pass
    # through, so nothing downstream (memory, AI prompt, RAG) ever sees them.
    messages = [m for m in messages if not is_system_notice(m.text)]
    return messages[-max(1, limit):]


def _read_labeled_messages(root) -> list[WhatsAppMessage]:
    """One-to-one shape: bubbles carry a "You:"/"<Contact>:" sender-label group.

    WhatsApp renders the conversation into the accessibility tree twice (seen
    live: every message came back duplicated), so rows are de-duplicated by
    on-screen position + text and sorted top-to-bottom so [-1] is the newest."""
    labels: list = []

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 45:
            return
        if _safe_control_type(ctrl) == "GroupControl":
            name = _safe_name(ctrl).rstrip()
            if name.endswith(":") and name != "Infobar Container":
                labels.append((ctrl, name))
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(root)

    rows: list[tuple[int, WhatsAppMessage]] = []
    seen: set[tuple[int, str]] = set()
    for label, sender_label in labels:
        sender_label = sender_label.strip()
        base_text = _extract_bubble_text(label)
        # Media + timestamp live on the whole bubble row (the label's parent),
        # not the sender-label group itself.
        try:
            row = label.GetParentControl() or label
        except Exception:  # noqa: BLE001 - stale element
            row = label
        display, media_kind, media_note, time_text, media_rect = _enrich_media(row, base_text)
        if not display:
            continue
        try:
            top = label.BoundingRectangle.top
        except Exception:  # noqa: BLE001
            top = 0
        key = (top, display[:24])
        if key in seen:
            continue
        seen.add(key)
        rows.append((top, WhatsAppMessage(
            sender=sender_label.rstrip(":").strip(),
            text=display,
            is_incoming=sender_label != _SELF_SENDER_LABEL,
            media_kind=media_kind,
            media_note=media_note,
            time_text=time_text,
            media_rect=media_rect,
        )))
    rows.sort(key=lambda r: r[0])
    return [m for _top, m in rows]


def _read_bubble_messages(root) -> list[WhatsAppMessage]:
    """Group shape: each message is a leaf DataItemControl row (outside the chat
    list). Who-sent-it isn't labelled, so it's inferred from horizontal
    alignment — our messages hug the right side of the conversation pane.

    WhatsApp renders the conversation into the accessibility tree twice, so rows
    are de-duplicated by their on-screen position + text."""
    try:
        rect = root.BoundingRectangle
        win_left, win_width = rect.left, rect.right - rect.left
    except Exception:  # noqa: BLE001
        win_left, win_width = 0, 0
    # A bubble whose text sits right of ~60% of the window width is right-aligned
    # — i.e. one we sent. (The DataItem *row* spans full width, so alignment must
    # come from the text controls, not the row.)
    outgoing_x = (win_left + win_width * 0.60) if win_width else None

    rows: list[tuple] = []
    seen: set[tuple[int, str]] = set()

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 50:
            return
        control_type = _safe_control_type(ctrl)
        if control_type == "DataGridControl" and _safe_name(ctrl) in ("Chat list", "Search results."):
            return  # the sidebar / search results, not messages
        if control_type == "DataItemControl" and not _contains_dataitem(ctrl):
            content = _bubble_item_content(ctrl)
            if content is not None:
                text, center_x, top, sender_hint, is_ours, media_kind, media_note, time_text, media_rect = content
                # Our own bubble's "You" sender label sometimes surfaces as its
                # own leaf row; it's not a message.
                if text.strip() == "You":
                    return
                key = (top, text[:24])
                if key not in seen:
                    seen.add(key)
                    rows.append((top, text, center_x, sender_hint, is_ours, media_kind, media_note, time_text, media_rect))
            return  # leaf message row — don't descend further
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(root)
    # Tree-walk order is not visual order (seen live: the newest message came
    # back mid-list). On screen, newer messages are lower — sort by vertical
    # position so [-1] really is the newest, which "reply to the newest
    # incoming message" depends on.
    rows.sort(key=lambda r: r[0])

    messages: list[WhatsAppMessage] = []
    carried_sender = ""
    for _top, text, center_x, sender_hint, is_ours, media_kind, media_note, time_text, media_rect in rows:
        if is_ours is True:  # the definitive "You:" label beats the alignment guess
            is_incoming = False
        else:
            is_incoming = not (outgoing_x is not None and center_x is not None and center_x > outgoing_x)
        if is_incoming:
            # In a group, only the first message of a sender's run carries the
            # name — follow-ups inherit it. Our own message ends the run.
            if sender_hint:
                carried_sender = sender_hint
            sender = carried_sender
        else:
            sender = "You"
            carried_sender = ""
        messages.append(WhatsAppMessage(
            sender=sender, text=text, is_incoming=is_incoming,
            media_kind=media_kind, media_note=media_note, time_text=time_text, media_rect=media_rect,
        ))
    return messages


def _contains_dataitem(ctrl, depth: int = 0) -> bool:
    if depth > 12:
        return False
    for child in _safe_children(ctrl):
        if _safe_control_type(child) == "DataItemControl":
            return True
        if _contains_dataitem(child, depth + 1):
            return True
    return False


# Buttons inside a message row that are actions/status, not a sender's name.
_NON_SENDER_BUTTON_WORDS = ("forward", "delivered", "read", "download", "play", "react", "reply")

# Marker texts that sit inside a bubble but aren't message content.
_MARKER_TEXTS = {"Read", "Edited"}

# System placeholders that render as a bubble but carry nothing readable —
# attributing or replying to them is always wrong, so the row is dropped.
_SYSTEM_NOTICE_PREFIXES = ("You received a view once message",)


def _bubble_item_sender(item) -> str:
    """The sender name shown on a group bubble, if this row carries one.

    In a group, the sender's name/avatar render as clickable ButtonControls on
    the FIRST message of that person's run (follow-up messages carry none —
    callers inherit the previous row's sender). Status buttons ("9:21 pm
    Delivered", "Forward media") are filtered out."""
    def walk(ctrl, depth=0):
        if depth > 8:
            return None
        if _safe_control_type(ctrl) == "ButtonControl":
            name = _safe_name(ctrl).strip()
            lowered = name.lower()
            if (
                name
                and len(name) <= 40
                and not _MESSAGE_TIME_RE.match(name)
                and not any(w in lowered for w in _NON_SENDER_BUTTON_WORDS)
            ):
                return name
        for child in _safe_children(ctrl):
            found = walk(child, depth + 1)
            if found:
                return found
        return None

    return walk(item) or ""


def _bubble_item_is_ours(item) -> Optional[bool]:
    """True when the row carries the definitive "You:" label group (our own
    message), None when there's no label — alignment decides then."""
    def walk(ctrl, depth=0):
        if depth > 8:
            return False
        if _safe_control_type(ctrl) == "GroupControl" and _safe_name(ctrl).strip() == _SELF_SENDER_LABEL:
            return True
        return any(walk(child, depth + 1) for child in _safe_children(ctrl))

    return True if walk(item) else None



def _iter_message_parts(ctrl, depth: int = 0) -> list:
    """The row's message content in document order: ("text", control) for
    TextControls and ("emoji", name) for inline emoji. WhatsApp renders emoji
    INSIDE a text message as ImageControls between the text runs (seen live:
    'works! ' + Image '🚀'), so reading only TextControls silently drops them.
    Quoted-reply subtrees are skipped; WhatsApp's own 'wds-ic-…' icon glyphs
    aren't emoji."""
    if depth > 8:
        return []
    control_type = _safe_control_type(ctrl)
    if control_type == "ButtonControl" and _safe_name(ctrl).startswith("Quoted"):
        return []
    found: list = []
    if control_type == "TextControl":
        found.append(("text", ctrl))
    elif control_type == "ImageControl":
        name = _safe_name(ctrl).strip()
        if name and len(name) <= 8 and not name.startswith("wds-ic"):
            found.append(("emoji", name))
    for child in _safe_children(ctrl):
        found.extend(_iter_message_parts(child, depth + 1))
    return found


def _iter_signal_names(ctrl, depth: int = 0) -> list[str]:
    """Lowercased names of the buttons/text/images/groups inside a bubble — the
    raw signal `_classify_media` matches attachment kinds against."""
    if depth > 10:
        return []
    names: list[str] = []
    if _safe_control_type(ctrl) in ("ButtonControl", "TextControl", "ImageControl", "GroupControl"):
        name = _safe_name(ctrl).strip().lower()
        if name:
            names.append(name)
    for child in _safe_children(ctrl):
        names.extend(_iter_signal_names(child, depth + 1))
    return names


def _media_note_for(kind: str, names: list[str], message_text: str) -> str:
    """The best detail the tree exposes for a media bubble: a voice/video
    duration, a document filename, or a photo/sticker/GIF caption."""
    tokens = [t for t in re.split(r"\s+", message_text.strip()) if t]
    if kind in (MEDIA_VOICE, MEDIA_VIDEO):
        for candidate in tokens + names:
            if _DURATION_RE.match(candidate):
                return candidate
        return ""
    if kind == MEDIA_DOCUMENT:
        for candidate in tokens + names:
            if _FILENAME_RE.match(candidate):
                return candidate
        return ""
    # photo / sticker / gif: whatever human caption rode alongside the media,
    # minus the kind label itself (a "Photo"-named ImageControl surfaces as a
    # text part) and any bare duration.
    label = _MEDIA_PLACEHOLDERS.get(kind, "").lower()
    caption = [t for t in tokens if t.lower() != label and not _DURATION_RE.match(t)]
    return " ".join(caption).strip()


def _classify_media(row, message_text: str) -> tuple[str, str]:
    """(media_kind, media_note) for a bubble, or ("", "") for a plain text
    message. Keys off the bubble's control names (Play/Download buttons, image
    names) — heuristic, tuned to WhatsApp Desktop's accessibility labels."""
    names = _iter_signal_names(row)
    for kind, signals in _MEDIA_SIGNALS:
        if any(any(sig in name for name in names) for sig in signals):
            return kind, _media_note_for(kind, names, message_text)
    return "", ""


def _bubble_time(row) -> str:
    """The bubble's own time label ("9:21 pm"), or "" if none is realized."""
    result = [""]

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 10 or result[0]:
            return
        if _TIME_LABEL_RE.match(_safe_name(ctrl).strip()):
            result[0] = _safe_name(ctrl).strip()
            return
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(row)
    return result[0]


def _largest_image_rect(row) -> Optional[tuple[int, int, int, int]]:
    """Screen rect of the biggest image/video control in a bubble (ignoring tiny
    emoji/icon glyphs), or None. Feeds both alignment inference and the optional
    thumbnail capture."""
    best: Optional[tuple[int, tuple[int, int, int, int]]] = None  # (area, rect)

    def walk(ctrl, depth: int = 0) -> None:
        nonlocal best
        if depth > 10:
            return
        if _safe_control_type(ctrl) == "ImageControl":
            try:
                r = ctrl.BoundingRectangle
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > 400 and (best is None or area > best[0]):
                    best = (area, (int(r.left), int(r.top), int(r.right), int(r.bottom)))
            except Exception:  # noqa: BLE001
                pass
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(row)
    return best[1] if best else None


def _media_center_x(row) -> Optional[float]:
    """Horizontal centre of the biggest picture in a bubble, used to judge
    left/right alignment (hence sender) when a media message has no text
    controls to measure. Sent media hugs the right edge, same as text."""
    rect = _largest_image_rect(row)
    return (rect[0] + rect[2]) / 2 if rect else None


# Picture-bearing kinds whose on-screen thumbnail can be captured to disk.
_PICTURE_KINDS = frozenset({MEDIA_PHOTO, MEDIA_VIDEO, MEDIA_STICKER, MEDIA_GIF})


def _enrich_media(row, base_text: str):
    """Fold attachment + timestamp detail into a message: returns
    (display_text, media_kind, media_note, time_text, media_rect). For a media
    bubble the display text becomes a readable placeholder ("[Voice note ·
    0:12]") so every downstream reader keeps working; a plain message passes
    through unchanged (media_rect None)."""
    time_text = _bubble_time(row)
    kind, note = _classify_media(row, base_text)
    if not kind:
        return base_text, "", "", time_text, None
    display = media_placeholder(kind, note) or base_text
    media_rect = _largest_image_rect(row) if kind in _PICTURE_KINDS else None
    return display, kind, note, time_text, media_rect


def _bubble_item_content(item):
    """(text, center_x, top, sender_hint, is_ours) of a group-chat bubble row.
    Text comes from the message TextControls (dropping the timestamp, the
    "Read"/"Edited" markers, and quoted-reply previews), falling back to emoji
    image names for emoji-only messages. Returns None when the row carries no
    message content. The center comes from the text controls (which are
    left/right aligned) rather than the full-width row."""
    parts: list[str] = []
    lefts: list[int] = []
    rights: list[int] = []
    for kind_tag, payload in _iter_message_parts(item):
        if kind_tag == "emoji":
            parts.append(payload)
            continue
        value = _safe_name(payload).strip()
        if not value or value in _MARKER_TEXTS or _MESSAGE_TIME_RE.match(value):
            continue
        parts.append(value)
        try:
            r = payload.BoundingRectangle
            lefts.append(r.left)
            rights.append(r.right)
        except Exception:  # noqa: BLE001
            pass

    text = " ".join(parts).strip()
    if text.startswith(_SYSTEM_NOTICE_PREFIXES):
        return None
    # Fold in attachment + time BEFORE the empty-text drop: a photo/voice bubble
    # has no text parts, so the old `if not text: return None` silently discarded
    # every media message — now it survives as a placeholder.
    display, media_kind, media_note, time_text, media_rect = _enrich_media(item, text)
    if not display:
        return None
    center_x = (min(lefts) + max(rights)) / 2 if lefts and rights else _media_center_x(item)
    try:
        top = int(item.BoundingRectangle.top)
    except Exception:  # noqa: BLE001
        top = 0
    return (display, center_x, top, _bubble_item_sender(item),
            _bubble_item_is_ours(item), media_kind, media_note, time_text, media_rect)


def _read_last_message_sync(window_handle: int) -> Optional[WhatsAppMessage]:
    """The newest message bubble in the open conversation, or None."""
    messages = _read_recent_messages_sync(window_handle, limit=1)
    return messages[-1] if messages else None


def _read_open_conversation_sync(window_handle: int, limit: int = 20):
    """(active_conversation_name, recent_messages) in a single tree read."""
    active = _get_active_conversation_name_sync(window_handle)
    messages = _read_recent_messages_sync(window_handle, limit)
    return active, messages


def _extract_bubble_text(sender_label_control) -> str:
    """Join the message-text TextControls that sit alongside `sender_label` in
    its parent row, skipping the sender label itself, the timestamp, the "Read"
    marker, and any quoted-reply preview."""
    try:
        row = sender_label_control.GetParentControl()
    except Exception:  # noqa: BLE001 - stale element
        return ""
    if row is None:
        return ""

    parts: list[str] = []
    for child in _safe_children(row):
        child_name = _safe_name(child).rstrip()
        if child_name.endswith(":"):
            continue  # the sender label group
        if _safe_control_type(child) == "ButtonControl" and child_name.startswith("Quoted"):
            continue  # the quoted original of a reply, not the new text
        for kind_tag, payload in _iter_message_parts(child):
            if kind_tag == "emoji":
                parts.append(payload)
                continue
            value = _safe_name(payload).strip()
            if not value or value in _MARKER_TEXTS or _MESSAGE_TIME_RE.match(value):
                continue
            parts.append(value)
    return " ".join(parts).strip()


def _iter_text_controls(ctrl, depth: int = 0) -> list:
    if depth > 8:
        return []
    found: list = []
    if _safe_control_type(ctrl) == "TextControl":
        found.append(ctrl)
    for child in _safe_children(ctrl):
        found.extend(_iter_text_controls(child, depth + 1))
    return found
