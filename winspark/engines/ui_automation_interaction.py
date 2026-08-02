"""Port of WinSpark.Infrastructure.Engines.UiAutomationInteractionEngine.

Uses the `uiautomation` package (comtypes-based UI Automation client, the
closest Python equivalent to .NET's System.Windows.Automation) in place of
the managed UIA wrapper. Every call is marshaled onto the STA thread via
StaAutomationThreadManager, matching the .NET version's threading model.

Faithful limitation carried over from the C# original: SetControlTextAsync
only works through ValuePattern. Modern rich-text controls (e.g. Windows 11's
WinUI-based Notepad, which uses a RichEdit/TextPattern surface with no
ValuePattern) are out of scope for this action in both the .NET and Python
versions — this was verified against a live example while building this
port, not assumed.
"""

from __future__ import annotations

import logging
from typing import Optional

from winspark.automation.sta_thread_manager import StaAutomationThreadManager
from winspark.domain.enums import ControlTypeKind
from winspark.domain.ui_automation import ControlLocator, UiControlInfo

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto

    _UIA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _UIA_AVAILABLE = False


class UiAutomationUnavailableError(RuntimeError):
    """Raised when the `uiautomation` package isn't available (i.e. not on Windows)."""


_KIND_TO_UIA_CONTROL_TYPE: dict = {}
_UIA_CONTROL_TYPE_TO_KIND: dict = {}

if _UIA_AVAILABLE:
    _KIND_TO_UIA_CONTROL_TYPE = {
        ControlTypeKind.BUTTON: auto.ControlType.ButtonControl,
        ControlTypeKind.TEXT_BOX: auto.ControlType.EditControl,
        ControlTypeKind.LIST: auto.ControlType.ListControl,
        ControlTypeKind.MENU: auto.ControlType.MenuControl,
        ControlTypeKind.TREE: auto.ControlType.TreeControl,
        ControlTypeKind.TAB: auto.ControlType.TabControl,
    }
    _UIA_CONTROL_TYPE_TO_KIND = {v: k for k, v in _KIND_TO_UIA_CONTROL_TYPE.items()}


def _safe_get(getter):
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


def _safe_get_bool(getter) -> bool:
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001
        return False


def _try_get_value(element) -> Optional[str]:
    try:
        pattern = element.GetPattern(auto.PatternId.ValuePattern)
        if pattern is not None:
            return pattern.Value
    except Exception:  # noqa: BLE001
        pass
    return None


def _map_element(element, depth: int) -> UiControlInfo:
    children: list[UiControlInfo] = []
    if depth < 3:
        for child in element.GetChildren():
            children.append(_map_element(child, depth + 1))

    return UiControlInfo(
        name=_safe_get(lambda: element.Name) or "",
        automation_id=_safe_get(lambda: element.AutomationId) or "",
        class_name=_safe_get(lambda: element.ClassName) or "",
        control_type=_UIA_CONTROL_TYPE_TO_KIND.get(_safe_get(lambda: element.ControlType), ControlTypeKind.OTHER),
        value=_try_get_value(element),
        is_enabled=_safe_get_bool(lambda: element.IsEnabled),
        children=tuple(children),
    )


class UiAutomationInteractionEngine:
    """Port of IUiAutomationInteractionEngine."""

    def __init__(self, sta_manager: StaAutomationThreadManager) -> None:
        self._sta_manager = sta_manager

    async def find_control_async(self, locator: ControlLocator) -> Optional[UiControlInfo]:
        def _find() -> Optional[UiControlInfo]:
            element = self._find_element_sync(locator)
            return _map_element(element, depth=0) if element is not None else None

        return await self._sta_manager.invoke_async(_find)

    async def read_control_value_async(self, locator: ControlLocator) -> Optional[str]:
        element = await self._find_element_async(locator)
        if element is None:
            return None
        return _try_get_value(element) or _safe_get(lambda: element.Name)

    async def focus_control_async(self, locator: ControlLocator) -> bool:
        element = await self._find_element_async(locator)
        if element is None:
            return False

        def _focus() -> bool:
            try:
                element.SetFocus()
                return True
            except Exception:  # noqa: BLE001
                return False

        return await self._sta_manager.invoke_async(_focus)

    async def set_control_text_async(self, locator: ControlLocator, text: str) -> bool:
        element = await self._find_element_async(locator)
        if element is None:
            return False

        def _set() -> bool:
            try:
                pattern = element.GetPattern(auto.PatternId.ValuePattern)
                if pattern is not None:
                    pattern.SetValue(text)
                    return True
            except Exception:  # noqa: BLE001
                logger.warning("SetControlText via ValuePattern failed", exc_info=True)
            return False

        return await self._sta_manager.invoke_async(_set)

    async def click_button_async(self, locator: ControlLocator) -> bool:
        element = await self._find_element_async(locator)
        if element is None:
            return False

        def _click() -> bool:
            try:
                pattern = element.GetPattern(auto.PatternId.InvokePattern)
                if pattern is not None:
                    pattern.Invoke()
                    return True
            except Exception:  # noqa: BLE001
                logger.warning("ClickButton failed", exc_info=True)
            return False

        return await self._sta_manager.invoke_async(_click)

    async def invoke_menu_async(self, locator: ControlLocator) -> bool:
        element = await self._find_element_async(locator)
        if element is None:
            return False

        def _invoke() -> bool:
            try:
                expand = element.GetPattern(auto.PatternId.ExpandCollapsePattern)
                if expand is not None:
                    expand.Expand()
                    return True
                invoke = element.GetPattern(auto.PatternId.InvokePattern)
                if invoke is not None:
                    invoke.Invoke()
                    return True
            except Exception:  # noqa: BLE001
                logger.warning("InvokeMenu failed", exc_info=True)
            return False

        return await self._sta_manager.invoke_async(_invoke)

    async def select_list_item_async(self, locator: ControlLocator, item_name: str) -> bool:
        element = await self._find_element_async(locator)
        if element is None:
            return False

        def _select() -> bool:
            try:
                if element.GetPattern(auto.PatternId.SelectionPattern) is None:
                    return False
                item = auto.Control(searchFromControl=element, ControlType=auto.ControlType.ListItemControl, Name=item_name)
                if not item.Exists(1, 0.2):
                    return False
                select_item = item.GetPattern(auto.PatternId.SelectionItemPattern)
                if select_item is not None:
                    select_item.Select()
                    return True
            except Exception:  # noqa: BLE001
                logger.warning("SelectListItem failed", exc_info=True)
            return False

        return await self._sta_manager.invoke_async(_select)

    async def select_tab_async(self, locator: ControlLocator, tab_name: str) -> bool:
        element = await self._find_element_async(locator)
        if element is None:
            return False

        def _select() -> bool:
            try:
                tab = auto.Control(searchFromControl=element, ControlType=auto.ControlType.TabItemControl, Name=tab_name)
                if not tab.Exists(1, 0.2):
                    return False
                select_item = tab.GetPattern(auto.PatternId.SelectionItemPattern)
                if select_item is not None:
                    select_item.Select()
                    return True
            except Exception:  # noqa: BLE001
                logger.warning("SelectTab failed", exc_info=True)
            return False

        return await self._sta_manager.invoke_async(_select)

    async def send_keyboard_input_async(self, window_handle: int, keys: str) -> bool:
        def _send() -> bool:
            auto.SetForegroundWindow(window_handle)
            auto.SendKeys(keys)
            return True

        return await self._sta_manager.invoke_async(_send)

    async def send_mouse_click_async(self, window_handle: int, x: int, y: int) -> bool:
        def _send() -> bool:
            auto.SetForegroundWindow(window_handle)
            auto.Click(x, y)
            return True

        return await self._sta_manager.invoke_async(_send)

    async def _find_element_async(self, locator: ControlLocator):
        return await self._sta_manager.invoke_async(lambda: self._find_element_sync(locator))

    @staticmethod
    def _find_element_sync(locator: ControlLocator):
        if not _UIA_AVAILABLE:
            raise UiAutomationUnavailableError("the 'uiautomation' package is required and only available on Windows")

        root = auto.ControlFromHandle(locator.window_handle)
        if root is None:
            return None

        search_kwargs: dict = {}
        if locator.automation_id:
            search_kwargs["AutomationId"] = locator.automation_id
        if locator.name:
            search_kwargs["Name"] = locator.name
        if locator.class_name:
            search_kwargs["ClassName"] = locator.class_name
        if locator.control_type is not None:
            search_kwargs["ControlType"] = _KIND_TO_UIA_CONTROL_TYPE.get(locator.control_type, auto.ControlType.CustomControl)

        if not search_kwargs:
            return root

        if locator.index is not None:
            search_kwargs["foundIndex"] = locator.index + 1  # uiautomation's foundIndex is 1-based

        found = auto.Control(searchFromControl=root, searchDepth=0xFFFFFFFF, **search_kwargs)
        return found if found.Exists(0, 0) else None
