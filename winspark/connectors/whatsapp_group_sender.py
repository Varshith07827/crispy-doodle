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
    _get_active_conversation_name_sync,
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

    async def open_chat_async(self, group_name: str) -> bool:
        """Bring `group_name` into view in WhatsApp (resolve it, then click it
        open). Foregrounds WhatsApp once; used by the "Open chat" button so the
        live message view reflects the selected chat."""
        window_handle, row = await self.resolve_chat_row_async(group_name)
        if window_handle is None or row is None:
            return False
        return await self._sta_manager.invoke_async(
            lambda: _open_chat_sync(window_handle, row.raw_text, group_name)
        )

    async def read_last_incoming_message_async(self, group_name: str) -> Optional[str]:
        """Open the chat and return the text of the newest message IF it was
        received from the other party. Returns None when the newest message is
        one we sent (so there's nothing new to reply to) or the chat/message
        can't be read. Used by OpenAI "reply" mode."""
        window_handle, row = await self.resolve_chat_row_async(group_name)
        if window_handle is None or row is None:
            return None

        opened = await self._sta_manager.invoke_async(
            lambda: _open_chat_sync(window_handle, row.raw_text, group_name)
        )
        if not opened:
            return None

        await asyncio.sleep(0.4)  # let the conversation's messages render
        message = await self._connector.read_last_message_async(window_handle)
        if message is None or not message.is_incoming:
            return None
        return message.text

    async def can_resolve_chat_async(self, group_name: str) -> bool:
        """Whether `group_name` can be found at all — in recents or via the
        search fallback. Tidies up any search it opened so WhatsApp is left on
        the recents list, making this safe to call from a "Check chat" button."""
        window_handle, row = await self.resolve_chat_row_async(group_name)
        if window_handle is not None:
            await self._sta_manager.invoke_async(lambda: _clear_search_sync(window_handle))
        return row is not None

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


def _send_unicode_text(text: str, interval: float = 0.01) -> None:
    """Type `text` via simulated Unicode keystrokes, correctly handling
    characters above U+FFFF (most emoji).

    `uiautomation.SendKeys`/`SendUnicodeChar` sends each character as a single
    16-bit KEYEVENTF_UNICODE scan code (`scan = ord(char)`). That's correct for
    BMP characters, but for an astral character like "💖" (U+1F496) it silently
    truncates the codepoint to its low 16 bits — confirmed live: typing a chat
    name containing an emoji into WhatsApp's search box produced "\\uf496"
    (0x1F496 & 0xFFFF), a string that matches nothing, leaving WhatsApp's UI
    stuck showing "No chats, contacts or messages found". The same truncation
    would corrupt any outgoing message containing an emoji.

    Windows' own text-input mechanism represents characters above U+FFFF as a
    UTF-16 surrogate pair — two 16-bit code units, each sent as its own
    KEYEVENTF_UNICODE keydown+keyup (this is how real IME-driven Unicode input,
    e.g. from AutoHotkey's Send, works). This splits any astral codepoint into
    its surrogate pair before sending, and passes BMP characters through as
    uiautomation already does."""
    _require_uia()
    for char in text:
        codepoint = ord(char)
        if codepoint > 0xFFFF:
            codepoint -= 0x10000
            units = (0xD800 + (codepoint >> 10), 0xDC00 + (codepoint & 0x3FF))
        else:
            units = (codepoint,)
        for unit in units:
            flag = auto.KeyboardEventFlag.KeyUnicode
            auto.SendInput(
                auto.KeyboardInput(0, unit, flag | auto.KeyboardEventFlag.KeyDown),
                auto.KeyboardInput(0, unit, flag | auto.KeyboardEventFlag.KeyUp),
            )
        time.sleep(interval)


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
    name against `chat_name`.

    A realized row is NOT necessarily a visible row: GridPattern also realizes
    rows below the list's visible area, with real screen coordinates far below
    the window (the grid's own rectangle is the full 50k-pixel virtual scroll
    content) — clicking one clamps the cursor to the screen edge and hits the
    bottom-most visible row instead (confirmed live). Scrolling the row into
    view first doesn't help either: Chromium animates the scroll, so the rect
    read before the click is stale by the time the click lands, hitting the
    adjacent row (also confirmed live). So: only rows whose click point is
    already inside the on-screen viewport are clicked directly; anything else
    goes through WhatsApp's search, whose results render at the top of the
    panel. Either way, the open is VERIFIED against the now-active
    conversation before reporting success — a wrong click can't masquerade as
    a successful open."""
    _require_uia()
    if not _ensure_foreground(window_handle):
        logger.warning("WhatsApp is not in the foreground; not clicking the chat row (would hit whatever window is on top)")
        return False

    target = chat_name.strip() or parse_chat_row(row_raw_text).get("chat_name", "")

    for grid_name in (_RECENTS_GRID, _SEARCH_RESULTS_GRID):
        chat_list = _find_chat_grid(window_handle, grid_name)
        if chat_list is None:
            continue
        item = _find_row_item(chat_list, row_raw_text, chat_name)
        if item is None or not _click_point_inside(item, chat_list, window_handle):
            continue  # not present / scrolled out of view — search brings it on screen
        if _click_item(item) and _opened_chat_matches(window_handle, target):
            return True
        break  # clicked but landed wrong — recover via search

    if not target:
        return False
    _search_and_read_rows_sync(window_handle, target)
    grid = _find_chat_grid(window_handle, _SEARCH_RESULTS_GRID)
    if grid is None:
        return False
    item = _find_row_item(grid, row_raw_text, target)
    if item is None or not _click_point_inside(item, grid, window_handle):
        return False
    if not _click_item(item):
        return False
    return _opened_chat_matches(window_handle, target)


def _opened_chat_matches(window_handle: int, target: str) -> bool:
    """Confirm the click actually opened `target` by reading which conversation
    the compose box now belongs to — and, when there is no compose box at all
    (announcement/read-only groups: confirmed live, they have none), by the
    conversation header title at the top of the right panel instead."""
    if not target:
        return True  # nothing to verify against — trust the click
    time.sleep(0.5)  # the compose placeholder swaps shortly after the click
    active = _get_active_conversation_name_sync(window_handle)
    if active and (active.strip().lower() == target.strip().lower() or chat_names_match(target, active)):
        return True
    return _conversation_header_matches(window_handle, target)


def _conversation_header_matches(window_handle: int, target: str) -> bool:
    """Whether the open conversation's header (top of the right panel) carries
    `target` as its title. The header button glues the title to a status line
    ("2023-27 Placements Announcements"), so a prefix match is accepted too —
    safe because the scan is confined to the header strip."""
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return False
    try:
        win_left, win_top, win_right, _win_bottom = win32gui.GetWindowRect(window_handle)
    except Exception:  # noqa: BLE001
        return False
    header_bottom = win_top + 170
    divider_x = win_left + (win_right - win_left) // 3  # right of the chat-list panel
    wanted = target.strip().lower()
    found: list[bool] = []

    def walk(ctrl, depth: int = 0) -> None:
        if found or depth > 40:
            return
        try:
            control_type = ctrl.ControlTypeName
        except Exception:  # noqa: BLE001 - stale element
            return
        if control_type in ("TextControl", "ButtonControl"):
            try:
                rect = ctrl.BoundingRectangle
                name = (ctrl.Name or "").strip()
            except Exception:  # noqa: BLE001
                rect, name = None, ""
            if rect is not None and name and rect.top < header_bottom and rect.left > divider_x:
                low = name.lower()
                if low == wanted or low.startswith(wanted + " ") or chat_names_match(target, name):
                    found.append(True)
                    return
        try:
            children = ctrl.GetChildren()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            walk(child, depth + 1)

    walk(root)
    return bool(found)


def _find_row_item(chat_list, row_raw_text: str, chat_name: str):
    for item in _iter_grid_row_controls(chat_list):
        name = (item.Name or "").strip()
        if not name:
            continue
        if name == row_raw_text.strip() or _row_matches_chat(name, chat_name):
            return item
    return None


def _click_point_inside(item, container, window_handle: int) -> bool:
    """Whether clicking the item's center would actually hit it.

    The container grid's BoundingRectangle is NOT the viewport — Chromium
    reports the full virtual scroll content (measured live: 52,927px tall for
    a 512-chat list, on a 1,200px screen). A realized-but-scrolled-away row
    has real coordinates thousands of pixels below the window; clicking there
    clamps the cursor to the screen edge and hits the bottom-most visible row
    instead — the original bug. So the check is against the intersection of
    the grid's rect and the window's actual on-screen rect."""
    try:
        r = item.BoundingRectangle
        c = container.BoundingRectangle
        win_left, win_top, win_right, win_bottom = win32gui.GetWindowRect(window_handle)
    except Exception:  # noqa: BLE001 - stale element / dead window
        return False
    if r.bottom - r.top < 8:
        return False  # collapsed/zero-height row — nothing real to click

    visible_left = max(c.left, win_left)
    visible_top = max(c.top, win_top)
    visible_right = min(c.right, win_right)
    visible_bottom = min(c.bottom, win_bottom)
    center_x = (r.left + r.right) // 2
    center_y = (r.top + r.bottom) // 2
    return visible_left <= center_x <= visible_right and (visible_top + 2) <= center_y <= (visible_bottom - 2)


def _click_item(item) -> bool:
    try:
        item.Click(simulateMove=False)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Failed to click chat row", exc_info=True)
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
            _send_unicode_text(query.strip())
            time.sleep(0.2)
        time.sleep(1.2)  # let the filtered results populate
    except Exception:  # noqa: BLE001
        logger.warning("WhatsApp search typing failed", exc_info=True)
        return []
    return _read_chat_rows_sync(window_handle, _SEARCH_RESULTS_GRID)


def _clear_search_sync(window_handle: int) -> None:
    """Exit an active search so WhatsApp returns to the recents list. Escape does
    this without needing to re-find the (now text-filled) search box.

    Guarded on an actually-active search: if no "Search results." grid is
    present we do nothing, so this never presses Escape while a normal chat is
    open (which would close it) — important because callers use it to tidy up
    after a resolve that may or may not have opened a search."""
    _require_uia()
    if _find_chat_grid(window_handle, _SEARCH_RESULTS_GRID) is None:
        return
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
    """Types real keystrokes via SendKeys/Unicode input rather than
    ValuePattern.SetValue — confirmed live that SetValue() silently no-ops on
    WhatsApp's compose box (it returns success but the text never appears),
    while simulated keystrokes do land and are verifiable via TextPattern.

    Keystrokes route to whichever window the OS considers foreground, not
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
            # _send_unicode_text (not uiautomation.SendKeys) — SendKeys truncates
            # any character above U+FFFF (most emoji) to 16 bits, corrupting the
            # message; see _send_unicode_text's docstring for the confirmed bug.
            _send_unicode_text(text)

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
