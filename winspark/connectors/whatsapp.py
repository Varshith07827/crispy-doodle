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


_SELF_SENDER_LABEL = "You:"
_MESSAGE_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[ap]m\b", re.IGNORECASE)


@dataclass(frozen=True)
class WhatsAppMessage:
    """One message bubble read from the open conversation."""

    sender: str
    text: str
    is_incoming: bool  # True = from the other party, False = sent by us ("You:")


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


def _read_chat_rows_sync(window_handle: int, grid_name: str = "Chat list") -> list[WhatsAppChatRow]:
    """Read rows from a WhatsApp chat-list-style grid. `grid_name` is "Chat list"
    for the recents sidebar, or "Search results." while a search is active (both
    are DataGrids with the same per-row accessible-name format, but expose their
    rows via different UIA mechanisms — see _iter_grid_row_controls)."""
    _require_win32()
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
        text = _extract_bubble_text(label)
        if not text:
            continue
        try:
            top = label.BoundingRectangle.top
        except Exception:  # noqa: BLE001
            top = 0
        key = (top, text[:24])
        if key in seen:
            continue
        seen.add(key)
        rows.append((top, WhatsAppMessage(
            sender=sender_label.rstrip(":").strip(),
            text=text,
            is_incoming=sender_label != _SELF_SENDER_LABEL,
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

    rows: list[tuple[int, str, Optional[float], str, Optional[bool]]] = []
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
                text, center_x, top, sender_hint, is_ours = content
                # Our own bubble's "You" sender label sometimes surfaces as its
                # own leaf row; it's not a message.
                if text.strip() == "You":
                    return
                key = (top, text[:24])
                if key not in seen:
                    seen.add(key)
                    rows.append((top, text, center_x, sender_hint, is_ours))
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
    for _top, text, center_x, sender_hint, is_ours in rows:
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
        messages.append(WhatsAppMessage(sender=sender, text=text, is_incoming=is_incoming))
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


def _bubble_item_emoji_text(item) -> str:
    """Emoji-only messages render as ImageControls with the emoji as the name
    (no TextControl at all) — pick those up so the message isn't dropped.
    WhatsApp's own icon glyphs are named 'wds-ic-…' and are skipped."""
    emoji: list[str] = []

    def walk(ctrl, depth=0):
        if depth > 8:
            return
        if _safe_control_type(ctrl) == "ImageControl":
            name = _safe_name(ctrl).strip()
            if name and len(name) <= 8 and not name.startswith("wds-ic"):
                emoji.append(name)
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(item)
    return "".join(emoji)


def _bubble_item_content(item):
    """(text, center_x, top, sender_hint, is_ours) of a group-chat bubble row.
    Text comes from the message TextControls (dropping the timestamp and the
    "Read" marker), falling back to emoji image names for emoji-only messages.
    Returns None when the row carries no message content. The center comes from
    the text controls (which are left/right aligned) rather than the
    full-width row."""
    parts: list[str] = []
    lefts: list[int] = []
    rights: list[int] = []
    for text_control in _iter_text_controls(item):
        value = _safe_name(text_control).strip()
        if not value or value == "Read" or _MESSAGE_TIME_RE.match(value):
            continue
        parts.append(value)
        try:
            r = text_control.BoundingRectangle
            lefts.append(r.left)
            rights.append(r.right)
        except Exception:  # noqa: BLE001
            pass

    text = " ".join(parts).strip()
    if not text:
        text = _bubble_item_emoji_text(item)
    if not text:
        return None
    center_x = (min(lefts) + max(rights)) / 2 if lefts and rights else None
    try:
        top = int(item.BoundingRectangle.top)
    except Exception:  # noqa: BLE001
        top = 0
    return text, center_x, top, _bubble_item_sender(item), _bubble_item_is_ours(item)


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
        for text_control in _iter_text_controls(child):
            value = _safe_name(text_control).strip()
            if not value or value == "Read" or _MESSAGE_TIME_RE.match(value):
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
