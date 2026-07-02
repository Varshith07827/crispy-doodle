"""Windows-only smoke test for WindowActionService (port of WindowActionService.cs).

Only exercises non-disruptive actions (invalid-handle rejection, activate/
bring-to-front on a real window) — deliberately avoids minimize/close/move
during automated runs since this talks to the real desktop session.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires a real Windows desktop session")


@pytest.mark.asyncio
async def test_invalid_handle_fails_cleanly():
    from winspark.engines.window_actions import WindowActionService

    service = WindowActionService()
    result = await service.activate_async(0)

    assert result.success is False
    assert result.error_message == "Invalid window handle."


@pytest.mark.asyncio
async def test_activate_and_bring_to_front_on_a_real_window_succeeds():
    from winspark.engines.window_actions import WindowActionService
    from winspark.engines.window_discovery import WindowDiscoveryEngine

    def fmt(process_name: str, title: str, pid: int) -> str:
        return process_name

    discovery = WindowDiscoveryEngine(name_formatter=fmt)
    snapshot = await discovery.discover()
    assert snapshot.windows, "expected at least one visible window on this desktop session"
    target = snapshot.windows[0]

    service = WindowActionService()
    result = await service.activate_async(target.handle)
    assert result.success is True

    result = await service.bring_to_front_async(target.handle)
    assert result.success is True
