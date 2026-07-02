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
    return match.group(1) if match else None


def _read_recent_messages_sync(window_handle: int, limit: int = 20) -> list[WhatsAppMessage]:
    """Read the most recent message bubbles (up to `limit`) from the open
    conversation, oldest-first.

    Structure, confirmed live: each bubble is a small subtree whose sender is a
    GroupControl named "You:" (sent by us) or "<Contact>:" (received), sitting
    as a sibling of the message-text TextControl and the timestamp TextControl.
    Bubbles appear top-to-bottom in chronological order; only the ones currently
    scrolled into view are realized in the accessibility tree, so this returns
    the visible tail of the conversation rather than its full history."""
    _require_win32()
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return []

    labels: list = []

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 45:
            return
        if ctrl.ControlTypeName == "GroupControl":
            name = (ctrl.Name or "").rstrip()
            if name.endswith(":") and name != "Infobar Container":
                labels.append(ctrl)
        for child in ctrl.GetChildren():
            walk(child, depth + 1)

    walk(root)

    messages: list[WhatsAppMessage] = []
    for label in labels[-max(1, limit):]:
        sender_label = (label.Name or "").strip()
        text = _extract_bubble_text(label)
        if not text:
            continue
        messages.append(
            WhatsAppMessage(
                sender=sender_label.rstrip(":").strip(),
                text=text,
                is_incoming=sender_label != _SELF_SENDER_LABEL,
            )
        )
    return messages


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
    row = sender_label_control.GetParentControl()
    if row is None:
        return ""

    parts: list[str] = []
    for child in row.GetChildren():
        child_name = (child.Name or "").rstrip()
        if child_name.endswith(":"):
            continue  # the sender label group
        if child.ControlTypeName == "ButtonControl" and (child.Name or "").startswith("Quoted"):
            continue  # the quoted original of a reply, not the new text
        for text_control in _iter_text_controls(child):
            value = (text_control.Name or "").strip()
            if not value or value == "Read" or _MESSAGE_TIME_RE.match(value):
                continue
            parts.append(value)
    return " ".join(parts).strip()


def _iter_text_controls(ctrl, depth: int = 0) -> list:
    if depth > 8:
        return []
    found: list = []
    if ctrl.ControlTypeName == "TextControl":
        found.append(ctrl)
    for child in ctrl.GetChildren():
        found.extend(_iter_text_controls(child, depth + 1))
    return found
