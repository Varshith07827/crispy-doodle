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


def _read_chat_rows_sync(window_handle: int) -> list[WhatsAppChatRow]:
    _require_win32()
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return []

    chat_list = auto.Control(
        searchFromControl=root, searchDepth=40, Name="Chat list", ControlType=auto.ControlType.DataGridControl
    )
    if not chat_list.Exists(2, 0.3):
        return []

    grid = chat_list.GetPattern(auto.PatternId.GridPattern)
    if grid is None:
        return []

    # grid.RowCount reflects the list's logical/total row count (from the DOM's
    # aria-rowcount), but GridPattern.GetItem() only succeeds for rows Chromium
    # has actually realized in the accessibility tree near the current scroll
    # position — unlike native virtualized UIA controls, it doesn't render
    # arbitrary rows on demand. GetItem raises COMError (E_INVALIDARG) once past
    # the realized range; confirmed live against a 511-chat list where only the
    # first several dozen rows were retrievable. So this reads whatever's
    # currently realized (i.e. what's scrolled into view) rather than the full
    # logical list.
    rows: list[WhatsAppChatRow] = []
    for row_index in range(grid.RowCount):
        try:
            item = grid.GetItem(row_index, 0)
        except Exception:  # noqa: BLE001 - comtypes.COMError past the realized range
            break
        name = (item.Name or "").strip() if item else ""
        if not name:
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
