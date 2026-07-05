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
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from winspark.ui.panels import (
    ActivityLogPanel,
    AutomationsPanel,
    GenericAppPanel,
    SettingsPanel,
    WhatsAppPanel,
)

_REFRESH_INTERVAL_MS = 1500
_APP_ROLE = Qt.UserRole


class MainWindow(QMainWindow):
    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._apps: list = []
        self._apps_signature: tuple = ()
        self._selected_key: Optional[str] = None
        # Which left-rail panel (not an app) is showing: "automations", "settings",
        # or None. Kept sticky so a background app-list rebuild — which can happen
        # when running an automation opens/closes a window — can't quietly flip
        # the view back to an app or the welcome page.
        self._active_rail: Optional[str] = None

        self.setWindowTitle("winSpark")
        from winspark.ui import branding

        self.setWindowIcon(branding.app_icon())
        self.setMinimumSize(760, 520)
        self._fit_to_screen(preferred_width=1160, preferred_height=760)

        # Left: running-apps sidebar (with a small header above the list)
        self._sidebar = QListWidget()
        self._sidebar.setObjectName("appSidebar")  # scopes the dark-navy QSS to just this list
        self._sidebar.setMinimumWidth(240)
        self._sidebar.currentItemChanged.connect(self._on_app_selected)
        # An explicit click is the ONLY thing allowed to leave a rail view
        # (Automations/Settings). Programmatic selection changes from rebuilding
        # the sidebar go through currentItemChanged, which ignores them while a
        # rail view is pinned — so a background rebuild can't bounce the user off
        # Automations. A real click here clears the pin and navigates.
        self._sidebar.itemClicked.connect(self._on_app_clicked)

        from PySide6.QtWidgets import QLabel

        sidebar_header = QLabel("OPEN APPLICATIONS")
        sidebar_header.setStyleSheet("color: #7c8aa0; font-weight: 600; font-size: 8pt; letter-spacing: 1px; padding: 12px 14px 6px 14px; background: transparent;")
        self._left = QWidget()
        left = self._left
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(sidebar_header)
        left_layout.addWidget(self._sidebar, 1)

        from PySide6.QtWidgets import QPushButton as _QPushButton

        rail_style = (
            "QPushButton { background: transparent; border: none; color: #cbd5e1; text-align: left;"
            " padding: 10px 16px; font-weight: 600; }"
            "QPushButton:hover { background: #1c2942; }"
        )
        automations_btn = _QPushButton("⚡  Automations")
        automations_btn.setStyleSheet(rail_style)
        automations_btn.clicked.connect(self._open_automations)
        left_layout.addWidget(automations_btn)

        settings_btn = _QPushButton("⚙  Settings")
        settings_btn.setStyleSheet(rail_style)
        settings_btn.clicked.connect(self._open_settings)
        left_layout.addWidget(settings_btn)
        left.setStyleSheet("background: #0f172a;")

        # Right: per-app panel (stacked) over the activity log
        self._whatsapp_panel = WhatsAppPanel(controller)
        self._generic_panel = GenericAppPanel(controller)
        self._settings_panel = SettingsPanel(controller)
        self._automations_panel = AutomationsPanel(controller)
        self._welcome = _welcome_widget()

        self._stack = QStackedWidget()
        self._stack.addWidget(self._welcome)            # 0
        self._stack.addWidget(self._whatsapp_panel)     # 1
        self._stack.addWidget(self._generic_panel)      # 2
        self._stack.addWidget(self._settings_panel)     # 3
        self._stack.addWidget(self._automations_panel)  # 4

        self._activity_panel = ActivityLogPanel(controller)

        # The panel and the activity log share the right side via a vertical
        # splitter, so the activity log is resizable by dragging — rather than a
        # fixed 3:2 split that can't be adjusted.
        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.addWidget(self._stack)
        right_splitter.addWidget(self._activity_panel)
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 2)
        right_splitter.setSizes([460, 260])
        right_splitter.setChildrenCollapsible(False)

        # Slim header above the workspace: two quiet toggles to give the panel
        # the whole window when you want it — hide the app list, hide the
        # activity feed. Both are checkable; teal = currently shown.
        from PySide6.QtWidgets import QHBoxLayout, QPushButton

        self._sidebar_toggle = QPushButton("☰  Apps")
        self._sidebar_toggle.setObjectName("flat")
        self._sidebar_toggle.setCheckable(True)
        self._sidebar_toggle.setChecked(True)
        self._sidebar_toggle.setToolTip("Show or hide the app list")
        self._sidebar_toggle.toggled.connect(self._left.setVisible)

        self._activity_toggle = QPushButton("Activity")
        self._activity_toggle.setObjectName("flat")
        self._activity_toggle.setCheckable(True)
        self._activity_toggle.setChecked(True)
        self._activity_toggle.setToolTip("Show or hide the activity feed")
        self._activity_toggle.toggled.connect(self._activity_panel.setVisible)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 6)
        header.addWidget(self._sidebar_toggle)
        header.addStretch(1)
        header.addWidget(self._activity_toggle)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(14, 8, 14, 10)
        right_layout.setSpacing(0)
        right_layout.addLayout(header)
        right_layout.addWidget(right_splitter)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 900])  # sidebar compact by default; user can drag
        self.setCentralWidget(splitter)

        self.statusBar().showMessage("Looking for your apps…")

        # Desktop notifications for screen watchers ("found it") — a tray icon
        # is what lets Windows show toast bubbles for us. Best effort: no tray,
        # no toasts (the Activity feed still records everything).
        from PySide6.QtWidgets import QStyle, QSystemTrayIcon

        self._tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self)
            self._tray.setToolTip("winSpark")
            self._tray.show()

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh_apps()
        self.refresh()

    def _fit_to_screen(self, preferred_width: int, preferred_height: int) -> None:
        """Size the window to fit the actual screen instead of a fixed guess.

        A hardcoded resize() was the real cause of buttons/dropdowns appearing
        to "disappear" off the right edge: on a screen whose available width
        (after Windows' DPI scaling) is smaller than the fixed size, the window
        itself extends past the visible screen — no amount of shrinking the
        widgets inside it helps, because the window frame itself is off-screen.
        Sizing against availableGeometry() and centering guarantees the whole
        window, and everything in it, starts fully visible on any display."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(preferred_width, preferred_height)
            return

        available = screen.availableGeometry()
        width = min(preferred_width, int(available.width() * 0.92))
        height = min(preferred_height, int(available.height() * 0.88))
        # Never go below the minimum size we declared, even on a tiny display —
        # the scroll areas inside each panel take over from there.
        width = max(width, self.minimumWidth())
        height = max(height, self.minimumHeight())
        self.resize(width, height)

        x = available.x() + (available.width() - width) // 2
        y = available.y() + (available.height() - height) // 2
        self.move(x, y)

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
        # Selection changed. If a rail view is pinned, ignore it entirely — only
        # an explicit click (_on_app_clicked) may leave a rail view. This is what
        # stops a background sidebar rebuild (or a deferred selection signal) from
        # bouncing the user off Automations/Settings onto Welcome.
        if self._active_rail is not None:
            return
        self._navigate_to_selected()

    def _on_app_clicked(self, _item) -> None:
        # A deliberate click on an app row leaves any rail view.
        if self._active_rail is not None:
            self._active_rail = None
            self._navigate_to_selected()

    def _navigate_to_selected(self) -> None:
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

    def _clear_app_selection(self) -> None:
        """Deselect the sidebar app (signals blocked) so opening a rail panel
        isn't immediately overridden by the selection handler or the periodic
        refresh re-selecting an app."""
        self._selected_key = None
        self._sidebar.blockSignals(True)
        self._sidebar.setCurrentItem(None)
        self._sidebar.blockSignals(False)

    def _open_settings(self) -> None:
        self._clear_app_selection()
        self._active_rail = "settings"
        self._settings_panel.reload()
        self._stack.setCurrentWidget(self._settings_panel)

    def _open_automations(self) -> None:
        self._clear_app_selection()
        self._active_rail = "automations"
        self._automations_panel.reload()
        self._stack.setCurrentWidget(self._automations_panel)

    # --- periodic refresh ----------------------------------------------

    def refresh(self) -> None:
        self.refresh_apps()
        self._activity_panel.refresh()
        # If a rail panel is meant to be showing, keep it showing — recover from
        # any stray swap so running an automation can never leave the user on the
        # wrong view.
        rail = {"automations": self._automations_panel, "settings": self._settings_panel}.get(self._active_rail)
        if rail is not None and self._stack.currentWidget() is not rail:
            self._stack.setCurrentWidget(rail)
        # Only the *cheap* refresh of the current panel on the timer (running
        # status etc.) — never the STA-backed chat-list reload.
        current = self._stack.currentWidget()
        if current is self._whatsapp_panel:
            self._whatsapp_panel.refresh()
        elif current is self._generic_panel:
            self._generic_panel.refresh_watchers()  # plain DB read — cheap
            fresh = self._selected_app()
            if fresh is not None and fresh.adapter_key is None:
                self._generic_panel.update_app_windows(fresh)  # windows come and go
        self._show_pending_notifications()
        self._update_status()

    def _show_pending_notifications(self) -> None:
        pop = getattr(self._controller, "pop_notifications", None)
        if pop is None:
            return
        for title, body in pop():
            if self._tray is not None:
                self._tray.showMessage(title, body)

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
    body = QLabel(
        "Select an application from the list to see what winSpark can do with it.\n\n"
        "Applications marked with a dot can be automated directly; the rest can still be read and assisted."
    )
    body.setWordWrap(True)
    layout.addStretch(1)
    layout.addWidget(title)
    layout.addWidget(body)
    layout.addStretch(2)
    return w
