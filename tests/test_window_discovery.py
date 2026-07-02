"""Windows-only smoke test for the pywin32-backed WindowDiscoveryEngine.

Skipped off-Windows since it depends on real EnumWindows/GetWindowPlacement
behavior that can't be faked meaningfully in CI on other platforms.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires a real Windows desktop session")


@pytest.mark.asyncio
async def test_discover_returns_live_windows():
    from winspark.engines.window_discovery import WindowDiscoveryEngine

    def fmt(process_name: str, title: str, pid: int) -> str:
        return process_name.replace(".exe", "")

    engine = WindowDiscoveryEngine(name_formatter=fmt)
    snapshot = await engine.discover()

    assert len(snapshot.windows) > 0
    assert snapshot.discovery_duration_seconds >= 0
    for window in snapshot.windows:
        assert window.title
        assert window.process_id > 0
        assert window.window_state.name in {"NORMAL", "MINIMIZED", "MAXIMIZED", "HIDDEN"}
