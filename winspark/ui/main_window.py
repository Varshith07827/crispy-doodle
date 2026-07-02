"""winSpark desktop control panel (PySide6) — the main window.

A tabbed front end over the whole platform: the AI fetch-webhook relay, live
WhatsApp chats, live window observation, and the event feed. Only the active
tab is refreshed on the timer (cheap), and the status bar summarizes engine
state. Depends only on the duck-typed controller (EngineHost in production).
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QTabWidget

from winspark.ui.panels import EventsPanel, RelayPanel, WhatsAppPanel, WindowsPanel

_REFRESH_INTERVAL_MS = 1500


class MainWindow(QMainWindow):
    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller

        self.setWindowTitle("winSpark")
        self.resize(1000, 680)

        self.relay_panel = RelayPanel(controller)
        self.whatsapp_panel = WhatsAppPanel(controller)
        self.windows_panel = WindowsPanel(controller)
        self.events_panel = EventsPanel(controller)

        self._tabs = QTabWidget()
        self._tabs.addTab(self.relay_panel, "AI Relay")
        self._tabs.addTab(self.whatsapp_panel, "WhatsApp")
        self._tabs.addTab(self.windows_panel, "Windows")
        self._tabs.addTab(self.events_panel, "Events")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self._tabs)

        self.statusBar().showMessage("Starting…")

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh()

    def _current_panel(self):
        return self._tabs.currentWidget()

    def _on_tab_changed(self, _index: int) -> None:
        self.refresh()

    def refresh(self) -> None:
        panel = self._current_panel()
        # WhatsApp chat reads drive the STA/UIA thread and are relatively slow;
        # only refresh that panel on an explicit tab switch or its own button,
        # not on every timer tick.
        if panel is self.whatsapp_panel and self.sender() is self._timer:
            self._update_status()
            return
        if panel is not None:
            panel.refresh()
        self._update_status()

    def _update_status(self) -> None:
        # Only cheap SQLite / in-memory reads here — this runs on the timer, and
        # STA-backed reads (WhatsApp) would block the Qt thread if a send is in
        # flight. WhatsApp running-state is shown on its own panel instead.
        relay = "on" if self._controller.is_relay_enabled() else "off"
        bindings = self._controller.get_bindings()
        enabled = sum(1 for b in bindings if b.is_enabled)
        windows = self._controller.get_windows()
        self.statusBar().showMessage(
            f"Relay: {relay}   |   Bindings: {len(bindings)} ({enabled} enabled)   |   Windows: {len(windows)}"
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().closeEvent(event)
