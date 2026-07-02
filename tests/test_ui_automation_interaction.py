"""Windows-only tests for UiAutomationInteractionEngine (port of
UiAutomationInteractionEngine.cs), verified against a native Win32 EDIT/BUTTON
control created and destroyed entirely within the test process — this avoids
touching any real application window on the host machine.

Note: this also documents a real, verified limitation carried over from the
.NET original — SetControlTextAsync only works via ValuePattern, which modern
rich-text controls (e.g. Windows 11's WinUI-based Notepad) don't expose. That
was confirmed against a live Notepad window while building this port; the
native EDIT control used here is deliberately the kind of target this action
actually supports (classic Win32 text boxes).
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires pywin32 + uiautomation on a real Windows session")


def _create_host_window():
    """Must run on the STA automation thread — a window only gets its
    messages pumped by whichever thread owns it, and UI Automation calls like
    SetFocus depend on that pump. Creating the window on the same thread that
    already pumps messages (the STA manager's worker loop) avoids the
    "target window never responds" deadlock this was built to route around;
    creating it on the test's asyncio thread instead reproduced that deadlock
    for real while building this port."""
    import win32api
    import win32con
    import win32gui

    hinst = win32api.GetModuleHandle(None)
    wc = win32gui.WNDCLASS()
    wc.hInstance = hinst
    wc.lpszClassName = "WinSparkUiaTestHost"
    wc.lpfnWndProc = {win32con.WM_DESTROY: lambda hwnd, msg, wparam, lparam: 0}
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error:
        pass  # already registered by an earlier test in this process

    hwnd = win32gui.CreateWindowEx(
        0, wc.lpszClassName, "WinSpark UIA Test Host", win32con.WS_OVERLAPPEDWINDOW,
        0, 0, 300, 200, 0, 0, hinst, None,
    )
    edit_hwnd = win32gui.CreateWindowEx(
        0, "EDIT", "initial", win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_BORDER,
        10, 10, 260, 30, hwnd, 0, hinst, None,
    )
    button_hwnd = win32gui.CreateWindowEx(
        0, "BUTTON", "Click Me", win32con.WS_CHILD | win32con.WS_VISIBLE, 10, 50, 100, 30, hwnd, 0, hinst, None,
    )
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    return hwnd, edit_hwnd, button_hwnd


def _destroy_window(hwnd):
    import win32gui

    win32gui.DestroyWindow(hwnd)


@pytest.fixture
async def host_window(manager):
    hwnd, edit_hwnd, button_hwnd = await manager.invoke_async(_create_host_window)
    try:
        yield hwnd, edit_hwnd, button_hwnd
    finally:
        await manager.invoke_async(lambda: _destroy_window(hwnd))


@pytest.fixture
def engine(manager):
    from winspark.engines.ui_automation_interaction import UiAutomationInteractionEngine

    return UiAutomationInteractionEngine(manager)


@pytest.mark.asyncio
async def test_find_control_by_class_name(engine, host_window):
    from winspark.domain.ui_automation import ControlLocator

    hwnd, edit_hwnd, _ = host_window
    info = await engine.find_control_async(ControlLocator(window_handle=hwnd, class_name="Edit"))

    assert info is not None
    assert info.class_name == "Edit"


@pytest.mark.asyncio
async def test_set_and_read_control_text_via_value_pattern(engine, host_window):
    from winspark.domain.ui_automation import ControlLocator

    hwnd, edit_hwnd, _ = host_window
    locator = ControlLocator(window_handle=hwnd, class_name="Edit")

    assert await engine.set_control_text_async(locator, "hello from winSpark port")
    value = await engine.read_control_value_async(locator)

    assert value == "hello from winSpark port"


@pytest.mark.asyncio
async def test_focus_control_succeeds(engine, host_window):
    from winspark.domain.ui_automation import ControlLocator

    hwnd, _, _ = host_window
    assert await engine.focus_control_async(ControlLocator(window_handle=hwnd, class_name="Edit"))


@pytest.mark.asyncio
async def test_click_button_invokes_it(engine, host_window):
    from winspark.domain.ui_automation import ControlLocator

    hwnd, _, button_hwnd = host_window
    locator = ControlLocator(window_handle=hwnd, class_name="Button")

    assert await engine.click_button_async(locator)


@pytest.mark.asyncio
async def test_find_control_returns_none_for_missing_locator(engine, host_window):
    from winspark.domain.ui_automation import ControlLocator

    hwnd, _, _ = host_window
    info = await engine.find_control_async(ControlLocator(window_handle=hwnd, automation_id="does-not-exist"))

    assert info is None
