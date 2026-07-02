"""Windows-only tests for TextInjectionEngine (port of TextInjectionEngine.cs),
the top of the InjectTextAction stack — exercised against a native Win32 EDIT
control created and destroyed within the test process (see
test_ui_automation_interaction.py for why: no real application window is
touched by this port's tests).
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires pywin32 + uiautomation on a real Windows session")


def _create_host_window():
    """Must run on the STA automation thread — see the identical helper (and
    its docstring) in test_ui_automation_interaction.py for why: a window
    only gets pumped by whichever thread owns it, and UI Automation calls
    depend on that pump."""
    import win32api
    import win32con
    import win32gui

    hinst = win32api.GetModuleHandle(None)
    wc = win32gui.WNDCLASS()
    wc.hInstance = hinst
    wc.lpszClassName = "WinSparkTextInjectionTestHost"
    wc.lpfnWndProc = {win32con.WM_DESTROY: lambda hwnd, msg, wparam, lparam: 0}
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error:
        pass

    hwnd = win32gui.CreateWindowEx(
        0, wc.lpszClassName, "WinSpark Text Injection Test Host", win32con.WS_OVERLAPPEDWINDOW,
        0, 0, 300, 200, 0, 0, hinst, None,
    )
    win32gui.CreateWindowEx(
        0, "EDIT", "", win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_BORDER, 10, 10, 260, 30, hwnd, 0, hinst, None,
    )
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    return hwnd


def _destroy_window(hwnd):
    import win32gui

    win32gui.DestroyWindow(hwnd)


@pytest.fixture
async def host_window(manager):
    hwnd = await manager.invoke_async(_create_host_window)
    try:
        yield hwnd
    finally:
        await manager.invoke_async(lambda: _destroy_window(hwnd))


@pytest.fixture
def text_injection(manager):
    from winspark.engines.text_injection import TextInjectionEngine
    from winspark.engines.ui_automation_interaction import UiAutomationInteractionEngine

    return TextInjectionEngine(UiAutomationInteractionEngine(manager))


@pytest.mark.asyncio
async def test_replace_text_sets_and_verifies_via_win32_readback(text_injection, host_window):
    import win32gui

    from winspark.domain.ui_automation import ControlLocator

    hwnd = host_window
    locator = ControlLocator(class_name="Edit")

    result = await text_injection.replace_text_async(hwnd, locator, "winSpark port verification")

    assert result.success is True
    assert result.applied_text == "winSpark port verification"

    edit_hwnd = win32gui.FindWindowEx(hwnd, 0, "EDIT", None)
    assert win32gui.GetWindowText(edit_hwnd) == "winSpark port verification"


@pytest.mark.asyncio
async def test_insert_text_appends_to_existing_value(text_injection, host_window):
    from winspark.domain.ui_automation import ControlLocator

    hwnd = host_window
    locator = ControlLocator(class_name="Edit")

    await text_injection.replace_text_async(hwnd, locator, "Hello")
    result = await text_injection.insert_text_async(hwnd, locator, " World")

    assert result.success is True
    assert result.applied_text == "Hello World"


@pytest.mark.asyncio
async def test_clear_text_empties_the_control(text_injection, host_window):
    from winspark.domain.ui_automation import ControlLocator

    hwnd = host_window
    locator = ControlLocator(class_name="Edit")

    await text_injection.replace_text_async(hwnd, locator, "something")
    result = await text_injection.clear_text_async(hwnd, locator)

    assert result.success is True
    assert result.applied_text == ""


@pytest.mark.asyncio
async def test_apply_fails_cleanly_when_control_not_found(text_injection, host_window):
    from winspark.domain.ui_automation import ControlLocator

    hwnd = host_window
    locator = ControlLocator(automation_id="does-not-exist")

    result = await text_injection.replace_text_async(hwnd, locator, "text")

    assert result.success is False
    assert "Unable to focus" in result.error_message
