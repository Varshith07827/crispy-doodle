"""Port of WinSpark.Infrastructure.Native.WindowEnumerator + Engines.WindowDiscoveryEngine.

Windows-only: uses pywin32 (win32gui/win32process) in place of the .NET
DllImport P/Invoke calls (EnumWindows, GetWindowText, GetForegroundWindow, ...).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

_OWN_PID = os.getpid()

from winspark.constants import DEFAULT_DISCOVERY_INTERVAL_SECONDS
from winspark.domain.enums import WindowStateKind
from winspark.domain.models import WindowDiscoverySnapshot, WindowInfo
from winspark.services.process_metrics import ProcessMetricsService

logger = logging.getLogger(__name__)

try:
    import win32con
    import win32gui
    import win32process

    _WIN32_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _WIN32_AVAILABLE = False

# Class of the per-app UWP surface (the cloaked/ghost window that shadows a
# Store app's real ApplicationFrameWindow). It's what briefly flashes into the
# app list as a UWP app closes — never the window we want to show.
_UWP_CORE_WINDOW_CLASS = "Windows.UI.Core.CoreWindow"

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000


def _window_app_id(hwnd: int) -> str:
    """The window's explicit AppUserModelID, or ''. Installed web apps
    (YouTube-as-an-app etc.) carry a distinct '..._crx_...' id here — measured
    live: a plain Chrome window reports 'Chrome', the YouTube app reports
    'Chrome._crx_<extension-id>'. Requires COM on this thread; failures just
    mean 'no explicit identity'."""
    try:
        import pythoncom
        from win32comext.propsys import propsys, pscon

        pythoncom.CoInitialize()
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        value = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        return value or ""
    except Exception:  # noqa: BLE001
        return ""


def _passes_alt_tab_rule(ex_style: int, has_owner: bool) -> bool:
    """Windows' own taskbar/Alt-Tab eligibility: tool windows and owned windows
    are hidden unless they explicitly opt back in with WS_EX_APPWINDOW.
    Measured live: this exactly separated the real apps (Chrome, WhatsApp,
    Word, …) from background utilities EnumWindows still reports as visible
    (an OEM helper, Command Palette, PowerToys Quick Access, Program Manager) —
    so winSpark's app list matches what the user sees in Task View."""
    is_app = bool(ex_style & _WS_EX_APPWINDOW)
    if (ex_style & _WS_EX_TOOLWINDOW) and not is_app:
        return False
    if has_owner and not is_app:
        return False
    return True


def _is_cloaked(hwnd: int) -> bool:
    """True if DWM is cloaking the window — i.e. it's not actually on screen
    (a backgrounded UWP app, a window on another virtual desktop, or a helper
    ghost like the input host). IsWindowVisible returns True for these, so
    without this check they'd show up in the app list as phantom entries."""
    import ctypes
    from ctypes import wintypes

    cloaked = ctypes.c_int(0)
    _DWMWA_CLOAKED = 14
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
    except Exception:  # noqa: BLE001 - treat an unqueryable window as not cloaked
        return False
    return cloaked.value != 0


class NativeWindowUnavailableError(RuntimeError):
    """Raised when pywin32 isn't available (i.e. not running on Windows)."""


def _enumerate_visible_windows() -> list[tuple[int, str, int, WindowStateKind, str]]:
    """Equivalent of WindowEnumerator.EnumerateVisibleWindows()."""
    if not _WIN32_AVAILABLE:
        raise NativeWindowUnavailableError("pywin32 is required and only available on Windows")

    results: list[tuple[int, str, int, WindowStateKind, str]] = []

    def _callback(hwnd: int, _: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True

        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return True

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid or pid == _OWN_PID:
            return True  # never list winSpark's own window in its app list

        iconic = win32gui.IsIconic(hwnd)

        # Skip UWP phantom windows: the per-app CoreWindow (the real Store/
        # Settings app is its ApplicationFrameWindow, shown separately), and any
        # window DWM is cloaking while not merely minimized — those are
        # background/other-desktop ghosts, not apps the user is looking at. A
        # minimized window is still a real app, so cloaked-and-iconic stays.
        if win32gui.GetClassName(hwnd) == _UWP_CORE_WINDOW_CLASS:
            return True
        if not iconic and _is_cloaked(hwnd):
            return True

        # Match Task View/Alt-Tab: skip tool windows and owned windows that
        # haven't opted in as app windows — background utilities the user
        # never sees as "open apps".
        ex_style = win32gui.GetWindowLong(hwnd, _GWL_EXSTYLE)
        has_owner = bool(win32gui.GetWindow(hwnd, win32con.GW_OWNER))
        if not _passes_alt_tab_rule(ex_style, has_owner):
            return True

        if iconic:
            state = WindowStateKind.MINIMIZED
        else:
            # pywin32's win32gui has no IsZoomed; GetWindowPlacement's showCmd
            # is the documented way to detect maximized state.
            show_cmd = win32gui.GetWindowPlacement(hwnd)[1]
            if show_cmd == win32con.SW_SHOWMAXIMIZED:
                state = WindowStateKind.MAXIMIZED
            else:
                state = WindowStateKind.NORMAL

        results.append((hwnd, title, pid, state, _window_app_id(hwnd)))
        return True

    win32gui.EnumWindows(_callback, None)
    return results


def _resolve_process_info(pid: int) -> tuple[str, str]:
    """Equivalent of WindowDiscoveryEngine.ResolveProcessInfo()."""
    import psutil

    try:
        proc = psutil.Process(pid)
        name = proc.name()
        if not name.lower().endswith(".exe"):
            name += ".exe"
        try:
            exe_path = proc.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            exe_path = ""
        return name, exe_path
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "unknown.exe", ""


class WindowDiscoveryEngine:
    """Port of IWindowDiscoveryEngine / WindowDiscoveryEngine.

    name_formatter: Callable[[process_name, title, pid], str] — port of
    IApplicationNameFormatter.FormatFriendlyName; pass a simple lambda for now.
    """

    def __init__(
        self,
        name_formatter: Callable[[str, str, int], str],
        discovery_interval_seconds: float = DEFAULT_DISCOVERY_INTERVAL_SECONDS,
    ) -> None:
        self._name_formatter = name_formatter
        self._interval = discovery_interval_seconds
        self._process_metrics = ProcessMetricsService()
        self._process_cache: dict[int, tuple[str, str]] = {}

        self.current_snapshot: Optional[WindowDiscoverySnapshot] = None
        self.is_running = False
        self._on_snapshot_updated: list[Callable[[WindowDiscoverySnapshot], None]] = []

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def on_snapshot_updated(self, callback: Callable[[WindowDiscoverySnapshot], None]) -> None:
        self._on_snapshot_updated.append(callback)

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_discovery_loop())
        logger.info("Window Discovery Engine started")

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self._stop_event.set()
        if self._task:
            await self._task
        logger.info("Window Discovery Engine stopped")

    async def discover(self) -> WindowDiscoverySnapshot:
        snapshot = await asyncio.to_thread(self._discover_internal)
        self.current_snapshot = snapshot
        return snapshot

    async def _run_discovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot = await self.discover()
                for callback in self._on_snapshot_updated:
                    callback(snapshot)
            except Exception:  # noqa: BLE001
                logger.exception("Window discovery cycle failed")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def _discover_internal(self) -> WindowDiscoverySnapshot:
        start = time.perf_counter()
        native_windows = _enumerate_visible_windows()
        foreground_handle = win32gui.GetForegroundWindow() if _WIN32_AVAILABLE else 0

        windows: list[WindowInfo] = []
        live_pids: set[int] = set()

        for handle, title, pid, state, app_id in native_windows:
            live_pids.add(pid)
            process_info = self._process_cache.get(pid)
            if process_info is None:
                process_info = _resolve_process_info(pid)
                self._process_cache[pid] = process_info
            process_name, executable_path = process_info

            metrics = self._process_metrics.get_metrics(pid)
            friendly_name = self._name_formatter(process_name, title, pid)

            windows.append(
                WindowInfo(
                    handle=handle,
                    title=title,
                    process_name=process_name,
                    friendly_name=friendly_name,
                    process_id=pid,
                    executable_path=executable_path,
                    memory_bytes=metrics.memory_bytes,
                    cpu_percent=metrics.cpu_percent,
                    start_time_utc=metrics.start_time_utc,
                    window_state=state,
                    is_active=(handle == foreground_handle),
                    app_user_model_id=app_id,
                )
            )

        self._process_metrics.prune_stale_samples(live_pids)
        stale_pids = set(self._process_cache) - live_pids
        for pid in stale_pids:
            del self._process_cache[pid]

        windows.sort(key=lambda w: (w.friendly_name, w.title))
        active_window = next((w for w in windows if w.is_active), None)

        duration = time.perf_counter() - start
        snapshot = WindowDiscoverySnapshot(
            windows=tuple(windows),
            active_window=active_window,
            captured_at_utc=datetime.now(timezone.utc),
            discovery_duration_seconds=duration,
        )
        logger.debug("Discovered %d windows in %.1fms", len(windows), duration * 1000)
        return snapshot
