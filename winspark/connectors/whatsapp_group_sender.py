"""WhatsApp group sender — resolves a chat by name and sends a message to it.

Not a port of the .NET IWhatsAppGroupSender implementation, which resolves a
"group name" to sidebar screen coordinates via OCR + visual detection and
caches a "sticky" click point. This uses the same GridPattern-based chat list
reader built for winspark/connectors/whatsapp.py: find the exact row by name,
then let `uiautomation`'s Control.Click() compute the click point from the
row's actual bounding rectangle — no OCR, no cached pixel coordinates that go
stale when the sidebar scrolls or resizes.

Sending is real UI automation into a real chat window. Nothing here is
exercised by an automated test that would actually deliver a message to a
real contact — see PORT_NOTES.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from winspark.automation.sta_thread_manager import StaAutomationThreadManager
from winspark.connectors.fetch_webhook_models import WhatsAppGroupSendResult
from winspark.connectors.whatsapp import (
    WhatsAppConnector,
    _find_chat_grid,
    _iter_grid_row_controls,
    _read_chat_rows_sync,
)
from winspark.connectors.whatsapp_chat_name_rules import chat_names_match
from winspark.connectors.whatsapp_row_parser import parse_chat_row

_RECENTS_GRID = "Chat list"
_SEARCH_RESULTS_GRID = "Search results."

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    import win32api
    import win32con
    import win32gui

    _UIA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _UIA_AVAILABLE = False


class WhatsAppUnavailableError(RuntimeError):
    """Raised when pywin32/uiautomation isn't available, or WhatsApp isn't running."""


def _ensure_foreground(hwnd: int, attempts: int = 5, settle: float = 0.2) -> bool:
    """Bring `hwnd` to the real OS foreground and CONFIRM it before returning.

    This is a safety gate, not a nicety. Everything downstream (opening a chat
    row, focusing/typing the compose box) is driven by physical mouse clicks at
    on-screen coordinates and by SendKeys, both of which act on whatever window
    is actually topmost/foreground — NOT on whichever element UI Automation
    points at. If WhatsApp isn't genuinely in front, a coordinate click lands on
    whatever window is visually on top at that spot (e.g. System Settings),
    activating it, and keystrokes get typed into it. So callers must abort when
    this returns False rather than clicking blind.

    A bare SetForegroundWindow is refused by Windows when the caller isn't
    already the foreground process — which is exactly what happened a few
    seconds after the user's last keypress (the scheduler polls on a background
    thread), so WhatsApp silently failed to open. This ports the .NET
    WhatsAppForegroundHelper technique: temporarily AttachThreadInput to the
    current foreground window's thread + AllowSetForegroundWindow, which makes
    the foreground change succeed, then verify via GetForegroundWindow and
    retry. Returns False (→ caller aborts) only if it genuinely can't."""
    if not _UIA_AVAILABLE:
        return False
    for _ in range(attempts):
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            _force_foreground(hwnd)
        except Exception:  # noqa: BLE001 - best effort; verified below
            pass
        time.sleep(settle)
    return win32gui.GetForegroundWindow() == hwnd


def _force_foreground(hwnd: int) -> None:
    """One attempt to make `hwnd` the foreground window.

    The decisive step is the phantom ALT tap: it makes Windows treat the
    following SetForegroundWindow as user-initiated and lifts the
    anti-focus-stealing lock that otherwise refuses it from a background
    process. Verified live on Windows 11 — this (ALT + SetForegroundWindow)
    reliably brought WhatsApp forward from a background thread with an unrelated
    window in front, where both a bare SetForegroundWindow and the
    AttachThreadInput technique failed. (AttachThreadInput actually *prevented*
    the change when combined with the ALT tap, so it's deliberately not used.)"""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if win32gui.GetForegroundWindow() == hwnd:
        return

    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - Windows may still decline; verified by the caller
        pass
    win32gui.BringWindowToTop(hwnd)


class WhatsAppGroupSender:
    def __init__(self, connector: WhatsAppConnector, sta_manager: StaAutomationThreadManager) -> None:
        self._connector = connector
        self._sta_manager = sta_manager  # must be the same STA thread instance the connector uses

    async def resolve_chat_row_async(self, group_name: str):
        """Find the chat: first in the recents sidebar, then — if it's not
        currently visible there — by typing into WhatsApp's search box and
        reading the results. Returns (window_handle, WhatsAppChatRow|None)."""
        window_handle = await self._connector.find_window_async()
        if window_handle is None:
            return None, None

        # 1. Recents (the currently-realized chat list).
        rows = await self._connector.read_chat_rows_async(window_handle)
        match = _match_chat_row(rows, group_name)
        if match is not None:
            return window_handle, match

        # 2. Fallback: search for it. Only happens when it wasn't in recents,
        #    so we don't disturb WhatsApp's UI in the common case.
        results = await self._sta_manager.invoke_async(
            lambda: _search_and_read_rows_sync(window_handle, group_name)
        )
        match = _match_chat_row(results, group_name)
        if match is not None:
            return window_handle, match

        # 3. Truly not found — don't leave WhatsApp stuck showing a search.
        await self._sta_manager.invoke_async(lambda: _clear_search_sync(window_handle))
        return window_handle, None

    async def send_to_group_async(self, group_name: str, message_text: str) -> WhatsAppGroupSendResult:
        window_handle, row = await self.resolve_chat_row_async(group_name)
        if window_handle is None:
            return WhatsAppGroupSendResult.failed("WhatsApp is not running.")
        if row is None:
            return WhatsAppGroupSendResult.failed(f"Chat '{group_name}' not found in the visible chat list.")

        opened = await self._sta_manager.invoke_async(lambda: _open_chat_sync(window_handle, row.raw_text, group_name))
        if not opened:
            return WhatsAppGroupSendResult.failed(f"Could not open chat '{group_name}'.")

        await asyncio.sleep(0.3)  # let the compose box swap over to the newly opened conversation

        active_name = await self._connector.get_active_conversation_name_async(window_handle)
        if active_name is None:
            return WhatsAppGroupSendResult.failed("Compose box not found after opening chat.")

        typed = await self._sta_manager.invoke_async(lambda: _set_compose_text_sync(window_handle, message_text))
        if not typed:
            return WhatsAppGroupSendResult.failed("Could not type into the compose box.")

        sent = await self._sta_manager.invoke_async(lambda: _send_compose_sync(window_handle))
        if not sent:
            return WhatsAppGroupSendResult.failed("Could not press Enter in the compose box.")

        # WhatsApp clears the compose box when a message is actually delivered, so an
        # empty box is our proof-of-send. Poll for it — the clear lags the keystroke —
        # and treat "still has text" as a genuine FAILURE, not a soft success. (The old
        # code reported 'sent-unverified' as success here, which is exactly why a
        # typed-but-not-sent message got marked SENT and never retried.)
        cleared = False
        for _ in range(10):
            await asyncio.sleep(0.25)
            if await self._sta_manager.invoke_async(lambda: _compose_is_empty_sync(window_handle)):
                cleared = True
                break

        if not cleared:
            return WhatsAppGroupSendResult.failed(
                "Message was typed but not sent — the compose box still has text after Enter."
            )

        return WhatsAppGroupSendResult.succeeded("sent", verified=True, appeared=True)


def _require_uia() -> None:
    if not _UIA_AVAILABLE:
        raise WhatsAppUnavailableError("the 'uiautomation' package is required and only available on Windows")


def _find_compose_element(window_handle: int):
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None
    compose = auto.Control(
        searchFromControl=root, searchDepth=40, ControlType=auto.ControlType.EditControl, RegexName=r"^Type a message"
    )
    return compose if compose.Exists(1, 0.2) else None


def _open_chat_sync(window_handle: int, row_raw_text: str, chat_name: str = "") -> bool:
    """Finds the chat row (fresh GridPattern lookup — grid items aren't stable
    across calls) and clicks it to open that conversation. Looks in both the
    recents grid and the search-results grid, so it works whether the chat was
    found directly or via the search fallback. Matches by exact row text, or —
    since search-result rows carry volatile previews — by the row's parsed chat
    name against `chat_name`."""
    _require_uia()

    for grid_name in (_RECENTS_GRID, _SEARCH_RESULTS_GRID):
        chat_list = _find_chat_grid(window_handle, grid_name)
        if chat_list is None:
            continue

        for item in _iter_grid_row_controls(chat_list):
            name = (item.Name or "").strip()
            if not name:
                continue
            if name == row_raw_text.strip() or _row_matches_chat(name, chat_name):
                if not _ensure_foreground(window_handle):
                    logger.warning("WhatsApp is not in the foreground; not clicking the chat row (would hit whatever window is on top)")
                    return False
                try:
                    item.Click(simulateMove=False)
                    return True
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to click chat row", exc_info=True)
                    return False

    return False


def _match_chat_row(rows: list, group_name: str):
    """Pick the row for `group_name` from a list of WhatsAppChatRow: exact
    (case-insensitive) first, then fuzzy (truncation-tolerant)."""
    target = group_name.strip().lower()
    for row in rows:
        if row.chat_name.strip().lower() == target:
            return row
    for row in rows:
        if chat_names_match(group_name, row.chat_name):
            return row
    return None


def _row_matches_chat(row_raw_text: str, chat_name: str) -> bool:
    if not chat_name:
        return False
    parsed = parse_chat_row(row_raw_text).get("chat_name", "")
    return parsed.strip().lower() == chat_name.strip().lower() or chat_names_match(chat_name, parsed)


def _find_search_box(window_handle: int):
    """Find WhatsApp's chat-search box. In the empty state its accessible name is
    "Search or start a new chat"; once it has a query typed in, the name becomes
    that query — so the empty-state match alone isn't enough. The fallback picks
    the top-most edit control that isn't the message compose box or the
    locked-chats search (the search box sits at the top of the left panel)."""
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None

    box = auto.Control(
        searchFromControl=root, searchDepth=40, ControlType=auto.ControlType.EditControl,
        RegexName=r"^Search or start",
    )
    if box.Exists(2, 0.3):
        return box

    best = None
    best_top = None

    def walk(ctrl, depth=0):
        nonlocal best, best_top
        if depth > 40:
            return
        if ctrl.ControlTypeName == "EditControl":
            name = ctrl.Name or ""
            if not name.startswith("Type a message") and name != "Search locked chats":
                try:
                    top = ctrl.BoundingRectangle.top
                except Exception:  # noqa: BLE001
                    top = 0
                if best_top is None or top < best_top:
                    best_top, best = top, ctrl
        for c in ctrl.GetChildren():
            walk(c, depth + 1)

    walk(root)
    return best


def _search_and_read_rows_sync(window_handle: int, query: str) -> list:
    """Type `query` into WhatsApp's search box and read the results grid.
    Verified live: search results appear in a separate DataGrid named
    "Search results." with the same per-row format as the recents list. Leaves
    the search active so _open_chat_sync can click the result (opening the chat
    clears the search)."""
    _require_uia()
    if not _ensure_foreground(window_handle):
        return []
    box = _find_search_box(window_handle)
    if box is None:
        return []
    try:
        box.SetFocus()
        box.Click(simulateMove=False)
        auto.SendKeys("{Ctrl}a", waitTime=0.1)
        auto.SendKeys("{Delete}", waitTime=0.1)
        if query.strip():
            auto.SendKeys(query.strip(), waitTime=0.2)
        time.sleep(1.2)  # let the filtered results populate
    except Exception:  # noqa: BLE001
        logger.warning("WhatsApp search typing failed", exc_info=True)
        return []
    return _read_chat_rows_sync(window_handle, _SEARCH_RESULTS_GRID)


def _clear_search_sync(window_handle: int) -> None:
    """Exit an active search so WhatsApp returns to the recents list. Escape does
    this without needing to re-find the (now text-filled) search box."""
    _require_uia()
    if not _ensure_foreground(window_handle):
        return
    box = _find_search_box(window_handle)
    try:
        if box is not None:
            box.Click(simulateMove=False)
        auto.SendKeys("{Ctrl}a", waitTime=0.1)
        auto.SendKeys("{Delete}", waitTime=0.1)
        auto.SendKeys("{Esc}", waitTime=0.1)
    except Exception:  # noqa: BLE001
        pass


def _read_compose_text(compose) -> str:
    """WhatsApp's compose box is a contenteditable div, not a native text
    control — its ValuePattern.Value is stale/disconnected (confirmed live:
    it read back a static '\\n' regardless of actual content, both before and
    after typing real text). TextPattern.DocumentRange.GetText() reads the
    real content correctly; an empty box reads as '\\n' too, so callers
    should compare against .strip()."""
    text_pattern = compose.GetPattern(auto.PatternId.TextPattern)
    if text_pattern is None:
        return ""
    return text_pattern.DocumentRange.GetText(-1) or ""


def _set_compose_text_sync(window_handle: int, text: str) -> bool:
    """Types real keystrokes via SendKeys rather than ValuePattern.SetValue —
    confirmed live that SetValue() silently no-ops on WhatsApp's compose box
    (it returns success but the text never appears), while simulated
    keystrokes do land and are verifiable via TextPattern.

    SendKeys routes to whichever window the OS considers foreground, not
    whichever element UI Automation calls SetFocus() on — confirmed live:
    without explicitly forcing the window to the real OS foreground first,
    keystrokes silently went nowhere (likely to the caller's own terminal
    window instead) even though every UIA call looked like it succeeded.
    """
    _require_uia()
    compose = _find_compose_element(window_handle)
    if compose is None:
        return False

    if not _ensure_foreground(window_handle):
        logger.warning("WhatsApp is not in the foreground; not typing (keystrokes would go to another window)")
        return False

    try:
        compose.SetFocus()
        compose.Click(simulateMove=False)

        if _read_compose_text(compose).strip():
            auto.SendKeys("{Ctrl}a", waitTime=0.15)
            auto.SendKeys("{Delete}", waitTime=0.15)

        if text:
            # uiautomation.SendKeys("") raises IndexError internally — an empty
            # target text needs no typing anyway, the Ctrl+A/Delete above already
            # cleared everything.
            auto.SendKeys(text, waitTime=0.2)

        # WhatsApp's React re-render lags slightly behind the keystroke itself —
        # confirmed live: reading back immediately after Delete sometimes still
        # showed the old text. A short settle delay before the verifying read
        # fixed it.
        time.sleep(0.3)
        return _read_compose_text(compose).strip() == text.strip()
    except Exception:  # noqa: BLE001
        logger.warning("Failed to set compose box text", exc_info=True)
        return False


def _send_compose_sync(window_handle: int) -> bool:
    _require_uia()
    compose = _find_compose_element(window_handle)
    if compose is None:
        return False
    if not _ensure_foreground(window_handle):
        logger.warning("WhatsApp is not in the foreground; not pressing Enter (would send in another window)")
        return False
    try:
        # SetFocus alone was not enough — confirmed live: the text typed fine but
        # Enter didn't send. A physical click (like typing does) is what actually
        # places the caret inside the contenteditable so WhatsApp treats Enter as
        # "send"; SetFocus focuses the element without a caret and Enter is ignored.
        compose.SetFocus()
        compose.Click(simulateMove=False)
        time.sleep(0.1)
        auto.SendKeys("{Enter}", waitTime=0.15)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Failed to send compose box message", exc_info=True)
        return False


def _compose_is_empty_sync(window_handle: int) -> bool:
    _require_uia()
    compose = _find_compose_element(window_handle)
    if compose is None:
        return False
    try:
        return not _read_compose_text(compose).strip()
    except Exception:  # noqa: BLE001
        return False
