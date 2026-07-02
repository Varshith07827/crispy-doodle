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
from winspark.connectors.whatsapp import WhatsAppConnector

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    import win32con
    import win32gui

    _UIA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _UIA_AVAILABLE = False


class WhatsAppUnavailableError(RuntimeError):
    """Raised when pywin32/uiautomation isn't available, or WhatsApp isn't running."""


def _ensure_foreground(hwnd: int, attempts: int = 6, settle: float = 0.15) -> bool:
    """Bring `hwnd` to the real OS foreground and CONFIRM it before returning.

    This is a safety gate, not a nicety. Everything downstream (opening a chat
    row, focusing/typing the compose box) is driven by physical mouse clicks at
    on-screen coordinates and by SendKeys, both of which act on whatever window
    is actually topmost/foreground — NOT on whichever element UI Automation
    points at. If WhatsApp isn't genuinely in front, a coordinate click lands on
    whatever window is visually on top at that spot (e.g. System Settings),
    activating it, and keystrokes get typed into it. So callers must abort when
    this returns False rather than clicking blind.

    SetForegroundWindow alone is unreliable (Windows refuses it from a
    non-foreground process), so we restore-if-minimized, ask for foreground,
    and verify via GetForegroundWindow, retrying briefly."""
    if not _UIA_AVAILABLE:
        return False
    for _ in range(attempts):
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001 - Windows declined the foreground change
            pass
        time.sleep(settle)
    return win32gui.GetForegroundWindow() == hwnd


class WhatsAppGroupSender:
    def __init__(self, connector: WhatsAppConnector, sta_manager: StaAutomationThreadManager) -> None:
        self._connector = connector
        self._sta_manager = sta_manager  # must be the same STA thread instance the connector uses

    async def resolve_chat_row_async(self, group_name: str):
        """Returns the matching WhatsAppChatRow, or None if not found in the
        currently-realized chat list range (see whatsapp.py's docstring)."""
        window_handle = await self._connector.find_window_async()
        if window_handle is None:
            return None, None

        rows = await self._connector.read_chat_rows_async(window_handle)
        target = group_name.strip().lower()
        for row in rows:
            if row.chat_name.strip().lower() == target:
                return window_handle, row
        return window_handle, None

    async def send_to_group_async(self, group_name: str, message_text: str) -> WhatsAppGroupSendResult:
        window_handle, row = await self.resolve_chat_row_async(group_name)
        if window_handle is None:
            return WhatsAppGroupSendResult.failed("WhatsApp is not running.")
        if row is None:
            return WhatsAppGroupSendResult.failed(f"Chat '{group_name}' not found in the visible chat list.")

        opened = await self._sta_manager.invoke_async(lambda: _open_chat_sync(window_handle, row.raw_text))
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
            return WhatsAppGroupSendResult.failed("Could not send the message (Enter key).")

        await asyncio.sleep(0.2)
        cleared = await self._sta_manager.invoke_async(lambda: _compose_is_empty_sync(window_handle))

        return WhatsAppGroupSendResult.succeeded(
            "sent" if cleared else "sent-unverified", verified=cleared, appeared=cleared
        )


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


def _open_chat_sync(window_handle: int, row_raw_text: str) -> bool:
    """Finds the chat row again (fresh GridPattern lookup — grid items aren't
    stable references across calls) and clicks it to open that conversation."""
    _require_uia()
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return False

    chat_list = auto.Control(searchFromControl=root, searchDepth=40, Name="Chat list", ControlType=auto.ControlType.DataGridControl)
    if not chat_list.Exists(2, 0.3):
        return False

    grid = chat_list.GetPattern(auto.PatternId.GridPattern)
    if grid is None:
        return False

    for row_index in range(grid.RowCount):
        try:
            item = grid.GetItem(row_index, 0)
        except Exception:  # noqa: BLE001 - past the realized range, see whatsapp.py
            break
        if item is not None and (item.Name or "").strip() == row_raw_text.strip():
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
        compose.SetFocus()
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
