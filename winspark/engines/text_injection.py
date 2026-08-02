"""Port of WinSpark.Infrastructure.Engines.TextInjectionEngine."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from winspark.domain.enums import TextInjectionMode
from winspark.domain.ui_automation import ControlLocator, TextInjectionRequest, TextInjectionResult, UiControlInfo
from winspark.engines.ui_automation_interaction import UiAutomationInteractionEngine


class TextInjectionEngine:
    """Port of ITextInjectionEngine."""

    def __init__(self, ui_automation: UiAutomationInteractionEngine) -> None:
        self._ui_automation = ui_automation

    async def find_input_control_async(self, window_handle: int, locator: ControlLocator) -> Optional[UiControlInfo]:
        return await self._ui_automation.find_control_async(replace(locator, window_handle=window_handle))

    async def inject_text_async(self, request: TextInjectionRequest) -> TextInjectionResult:
        if request.mode == TextInjectionMode.INSERT:
            return await self.insert_text_async(request.window_handle, request.locator, request.text)
        if request.mode == TextInjectionMode.APPEND:
            return await self.append_text_async(request.window_handle, request.locator, request.text)
        if request.mode == TextInjectionMode.REPLACE:
            return await self.replace_text_async(request.window_handle, request.locator, request.text)
        if request.mode == TextInjectionMode.CLEAR:
            return await self.clear_text_async(request.window_handle, request.locator)
        return TextInjectionResult.failed("Unsupported injection mode.")

    async def insert_text_async(self, window_handle: int, locator: ControlLocator, text: str) -> TextInjectionResult:
        current = await self._ui_automation.read_control_value_async(replace(locator, window_handle=window_handle))
        combined = (current or "") + text
        return await self._apply_text_async(window_handle, locator, combined)

    async def append_text_async(self, window_handle: int, locator: ControlLocator, text: str) -> TextInjectionResult:
        return await self.insert_text_async(window_handle, locator, text)

    async def replace_text_async(self, window_handle: int, locator: ControlLocator, text: str) -> TextInjectionResult:
        return await self._apply_text_async(window_handle, locator, text)

    async def clear_text_async(self, window_handle: int, locator: ControlLocator) -> TextInjectionResult:
        return await self._apply_text_async(window_handle, locator, "")

    async def send_enter_key_async(self, window_handle: int, locator: ControlLocator) -> bool:
        target = replace(locator, window_handle=window_handle)
        await self._ui_automation.focus_control_async(target)
        return await self._ui_automation.send_keyboard_input_async(window_handle, "\r")

    async def _apply_text_async(self, window_handle: int, locator: ControlLocator, text: str) -> TextInjectionResult:
        target = replace(locator, window_handle=window_handle)

        if not await self._ui_automation.focus_control_async(target):
            return TextInjectionResult.failed("Unable to focus target control.")

        if not await self._ui_automation.set_control_text_async(target, text):
            return TextInjectionResult.failed("Unable to set control text via UI Automation.")

        return TextInjectionResult.succeeded(text)
