"""Entry point for the winSpark desktop control panel.

Run from winspark_py/:  python -m winspark.ui

Starts the Fetch-Webhook relay engine in-process (background asyncio thread)
and opens the PySide6 control panel. Toggling the relay on begins polling any
enabled bindings and relaying responses into WhatsApp for real, so it's left
off until you start it from the window (or via `python -m winspark.cli relay
enable` before launching).
"""

from __future__ import annotations

import logging
import sys


def _set_dpi_awareness() -> None:
    """Tell Windows this process handles its own per-monitor DPI scaling,
    BEFORE Qt starts (and before it tries and sometimes fails to do the same —
    seen live as "SetProcessDpiAwarenessContext() failed: Access is denied").

    Without this, Windows can DPI-virtualize the window: it reports/renders
    the window in a different coordinate space than Qt's own logical layout,
    so widgets Qt places well inside the window (like the row-1 buttons) can
    end up drawn outside the visible area, or the whole window renders
    blurry/scaled. Doing it here ourselves — before QApplication exists, and
    before some other component can set it first and make Qt's later attempt
    fail — is the standard fix; if it fails anyway (locked-down environments),
    we fall back through progressively older APIs rather than raising, since a
    slightly wrong DPI mode is better than a crash on startup."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        # PER_MONITOR_AWARE_V2 (-4) — matches what Qt itself defaults to.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _set_dpi_awareness()

    from winspark.ui import branding

    # Claim our own taskbar identity BEFORE any window exists, so the app shows
    # as winSpark (with our icon) instead of "Python".
    branding.set_windows_app_identity()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from winspark.data.connection import ConnectionFactory, default_database_path
    from winspark.ui.engine_host import EngineHost
    from winspark.ui.main_window import MainWindow

    db_path = default_database_path()
    connection_factory = ConnectionFactory(db_path)
    connection_factory.initialize_schema()
    logging.info("SQLite database at %s", db_path)

    host = EngineHost(connection_factory)
    host.start()

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(branding.APP_NAME)
    app.setApplicationDisplayName(branding.APP_NAME)
    app.setWindowIcon(branding.app_icon())

    from winspark.ui.theme import apply_theme

    apply_theme(app)

    window = MainWindow(host)
    window.show()

    # Ctrl+C in the launching terminal should quit cleanly. Without this, the
    # interrupt lands inside whatever Qt timer callback is running (seen live:
    # KeyboardInterrupt tracebacks out of refresh(), needing several presses).
    # Python only delivers signals while its interpreter is running, so a tiny
    # heartbeat timer keeps Qt's loop returning to Python often enough.
    import signal

    from PySide6.QtCore import QTimer

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    sigint_heartbeat = QTimer()
    sigint_heartbeat.timeout.connect(lambda: None)
    sigint_heartbeat.start(200)

    try:
        return app.exec()
    finally:
        host.shutdown()


if __name__ == "__main__":
    sys.exit(main())
