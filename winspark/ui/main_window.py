"""winSpark main window — the product shell.

Left: a live list of the apps you have open (refreshes as apps come and go).
Right: pick an app to see what winSpark can do with it — a guided setup for
apps it can automate (WhatsApp today), or a simple "observing" view for the
rest. Below: a plain-English activity feed.

The shell is app-agnostic: it asks the controller which apps are running and
whether each has an automation adapter, and shows the matching panel. Adding a
new adapter later needs no change here.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from winspark.ui.panels import ActivityLogPanel, GenericAppPanel, WhatsAppPanel

_REFRESH_INTERVAL_MS = 1500
_APP_ROLE = Qt.UserRole


class MainWindow(QMainWindow):
    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._apps: list = []
        self._apps_signature: tuple = ()
        self._selected_key: Optional[str] = None

        self.setWindowTitle("winSpark")
        self.resize(1040, 720)

        # Left: running-apps sidebar
        self._sidebar = QListWidget()
        self._sidebar.setMinimumWidth(240)
        self._sidebar.currentItemChanged.connect(self._on_app_selected)

        # Right: per-app panel (stacked) over the activity log
        self._whatsapp_panel = WhatsAppPanel(controller)
        self._generic_panel = GenericAppPanel(controller)
        self._welcome = _welcome_widget()

        self._stack = QStackedWidget()
        self._stack.addWidget(self._welcome)         # 0
        self._stack.addWidget(self._whatsapp_panel)  # 1
        self._stack.addWidget(self._generic_panel)   # 2

        self._activity_panel = ActivityLogPanel(controller)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self._stack, 3)
        right_layout.addWidget(self._activity_panel, 2)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self.statusBar().showMessage("Looking for your apps…")

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh_apps()
        self.refresh()

    # --- apps sidebar ---------------------------------------------------

    def refresh_apps(self) -> None:
        self._apps = list(self._controller.get_running_apps())
        signature = tuple((a.adapter_key or a.process_name, a.display_name, a.supported) for a in self._apps)
        if signature == self._apps_signature:
            return  # nothing changed — don't disturb selection
        self._apps_signature = signature

        self._sidebar.blockSignals(True)
        self._sidebar.clear()
        for app in self._apps:
            label = app.display_name if app.supported else f"{app.display_name}"
            item = QListWidgetItem(("●  " if app.supported else "   ") + label)
            item.setData(_APP_ROLE, self._key(app))
            if not app.supported:
                item.setForeground(Qt.gray)
            self._sidebar.addItem(item)
        self._restore_selection()
        self._sidebar.blockSignals(False)

    def _key(self, app) -> str:
        return app.adapter_key or app.process_name

    def _restore_selection(self) -> None:
        if self._selected_key is None:
            return
        for i in range(self._sidebar.count()):
            if self._sidebar.item(i).data(_APP_ROLE) == self._selected_key:
                self._sidebar.setCurrentRow(i)
                return

    def _selected_app(self):
        item = self._sidebar.currentItem()
        if item is None:
            return None
        key = item.data(_APP_ROLE)
        return next((a for a in self._apps if self._key(a) == key), None)

    def _on_app_selected(self, current, _previous) -> None:
        app = self._selected_app()
        self._selected_key = self._key(app) if app is not None else None
        if app is None:
            self._stack.setCurrentWidget(self._welcome)
        elif app.adapter_key == "whatsapp":
            self._stack.setCurrentWidget(self._whatsapp_panel)
            self._whatsapp_panel.refresh_chats()  # STA-backed — only on explicit selection
        else:
            self._generic_panel.set_app(app)
            self._stack.setCurrentWidget(self._generic_panel)

    # --- periodic refresh ----------------------------------------------

    def refresh(self) -> None:
        self.refresh_apps()
        self._activity_panel.refresh()
        # Only the *cheap* refresh of the current panel on the timer (running
        # status etc.) — never the STA-backed chat-list reload.
        current = self._stack.currentWidget()
        if current is self._whatsapp_panel:
            self._whatsapp_panel.refresh()
        self._update_status()

    def _update_status(self) -> None:
        running = "on" if self._controller.is_relay_enabled() else "off"
        self.statusBar().showMessage(f"{len(self._apps)} apps open   |   Automation: {running}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().closeEvent(event)


def _welcome_widget() -> QWidget:
    from PySide6.QtWidgets import QLabel

    w = QWidget()
    layout = QVBoxLayout(w)
    title = QLabel("<h2>Welcome to winSpark</h2>")
    body = QLabel("Pick an app on the left to get started.\n\nApps winSpark can automate are marked with a dot.")
    body.setWordWrap(True)
    layout.addStretch(1)
    layout.addWidget(title)
    layout.addWidget(body)
    layout.addStretch(2)
    return w
