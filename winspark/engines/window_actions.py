"""Port of WinSpark.Infrastructure.Engines.WindowActionService.

Unlike UI Automation (element tree walking / text injection), these are plain
User32 calls (SetForegroundWindow, ShowWindow, SendMessage, SetWindowPos) with
no COM apartment requirement — the .NET version only marshals them onto a
dedicated STA thread because its WPF UI thread needs it, not because Win32
itself does. Safe to call directly from any thread here.
"""

from __future__ import annotations

import asyncio

from winspark.domain.automation import WindowActionRequest, WindowActionResult
from winspark.domain.enums import WindowActionKind

try:
    import win32con
    import win32gui

    _WIN32_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _WIN32_AVAILABLE = False


class NativeWindowUnavailableError(RuntimeError):
    """Raised when pywin32 isn't available (i.e. not running on Windows)."""


class WindowActionService:
    """Port of IWindowActionService."""

    async def execute_async(self, request: WindowActionRequest) -> WindowActionResult:
        return await asyncio.to_thread(self._execute_internal, request)

    async def bring_to_front_async(self, window_handle: int) -> WindowActionResult:
        return await self.execute_async(WindowActionRequest(window_handle, WindowActionKind.BRING_TO_FRONT))

    async def activate_async(self, window_handle: int) -> WindowActionResult:
        return await self.execute_async(WindowActionRequest(window_handle, WindowActionKind.ACTIVATE))

    async def minimize_async(self, window_handle: int) -> WindowActionResult:
        return await self.execute_async(WindowActionRequest(window_handle, WindowActionKind.MINIMIZE))

    async def maximize_async(self, window_handle: int) -> WindowActionResult:
        return await self.execute_async(WindowActionRequest(window_handle, WindowActionKind.MAXIMIZE))

    async def restore_async(self, window_handle: int) -> WindowActionResult:
        return await self.execute_async(WindowActionRequest(window_handle, WindowActionKind.RESTORE))

    async def close_async(self, window_handle: int) -> WindowActionResult:
        return await self.execute_async(WindowActionRequest(window_handle, WindowActionKind.CLOSE))

    async def move_async(self, window_handle: int, x: int, y: int) -> WindowActionResult:
        return await self.execute_async(WindowActionRequest(window_handle, WindowActionKind.MOVE, x=x, y=y))

    async def resize_async(self, window_handle: int, width: int, height: int) -> WindowActionResult:
        return await self.execute_async(
            WindowActionRequest(window_handle, WindowActionKind.RESIZE, width=width, height=height)
        )

    @staticmethod
    def _execute_internal(request: WindowActionRequest) -> WindowActionResult:
        if not _WIN32_AVAILABLE:
            raise NativeWindowUnavailableError("pywin32 is required and only available on Windows")

        hwnd = request.window_handle
        if not win32gui.IsWindow(hwnd):
            return WindowActionResult.failed("Invalid window handle.")

        try:
            action = request.action
            if action == WindowActionKind.BRING_TO_FRONT:
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            elif action == WindowActionKind.ACTIVATE:
                win32gui.SetForegroundWindow(hwnd)
            elif action == WindowActionKind.MINIMIZE:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWMINIMIZED)
            elif action == WindowActionKind.MAXIMIZE:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWMAXIMIZED)
            elif action == WindowActionKind.RESTORE:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            elif action == WindowActionKind.CLOSE:
                win32gui.SendMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            elif action == WindowActionKind.MOVE:
                if request.x is None or request.y is None:
                    return WindowActionResult.failed("Move requires X and Y coordinates.")
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOP, request.x, request.y, 0, 0,
                    win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
            elif action == WindowActionKind.RESIZE:
                if request.width is None or request.height is None:
                    return WindowActionResult.failed("Resize requires Width and Height.")
                left, top, _, _ = win32gui.GetWindowRect(hwnd)
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOP, left, top, request.width, request.height,
                    win32con.SWP_SHOWWINDOW,
                )
            else:
                return WindowActionResult.failed(f"Unsupported action: {action}")

            return WindowActionResult.succeeded()
        except Exception as ex:  # noqa: BLE001
            return WindowActionResult.failed(str(ex))
