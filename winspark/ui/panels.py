"""The winSpark control-panel tabs (PySide6).

Each panel is a self-contained QWidget with a `refresh()` that repopulates from
the controller, and UI-logic methods kept separate from the modal dialogs that
call them so they can be driven directly in headless tests. Every panel depends
only on the small duck-typed controller (EngineHost in production, a fake in
tests).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from winspark.ui.dialogs import AddBindingDialog


def _table(columns: list[str], stretch_col: int) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    return table


def _fill(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setRowCount(len(rows))
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            table.setItem(r, c, QTableWidgetItem(value))


# --------------------------------------------------------------------------- #
# Relay
# --------------------------------------------------------------------------- #

_BINDING_COLUMNS = ["Group", "URL", "Interval", "Enabled", "State", "Polls", "Sent"]
_MESSAGE_COLUMNS = ["State", "Group", "Fetched (UTC)", "Message"]


class RelayPanel(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._bindings: list = []

        self._relay_label = QLabel()
        self._relay_button = QPushButton()
        self._relay_button.clicked.connect(self.toggle_relay)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Fetch-Webhook relay</b>"))
        header.addWidget(self._relay_label, 1)
        header.addWidget(self._relay_button)

        self._bindings_table = _table(_BINDING_COLUMNS, stretch_col=1)

        add_btn = QPushButton("Add / update…")
        add_btn.clicked.connect(self._on_add_clicked)
        enable_btn = QPushButton("Enable")
        enable_btn.clicked.connect(lambda: self.set_selected_enabled(True))
        disable_btn = QPushButton("Disable")
        disable_btn.clicked.connect(lambda: self.set_selected_enabled(False))
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove_clicked)
        test_btn = QPushButton("Queue test…")
        test_btn.clicked.connect(self._on_send_test_clicked)

        buttons = QHBoxLayout()
        for btn in (add_btn, enable_btn, disable_btn, remove_btn, test_btn):
            buttons.addWidget(btn)
        buttons.addStretch(1)

        self._messages_table = _table(_MESSAGE_COLUMNS, stretch_col=3)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(QLabel("Bindings"))
        layout.addWidget(self._bindings_table, 2)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("Recent relayed messages"))
        layout.addWidget(self._messages_table, 1)

    def refresh(self) -> None:
        enabled = self._controller.is_relay_enabled()
        self._relay_label.setText("running — polling active" if enabled else "stopped")
        self._relay_button.setText("Stop relay" if enabled else "Start relay")

        self._bindings = list(self._controller.get_bindings())
        _fill(
            self._bindings_table,
            [
                [
                    b.group_name,
                    b.fetch_url,
                    f"{b.poll_interval_seconds}s",
                    "yes" if b.is_enabled else "no",
                    b.last_fetch_state or "—",
                    str(b.total_polls),
                    str(b.total_sent),
                ]
                for b in self._bindings
            ],
        )

        messages = self._controller.get_recent_messages(30)
        group_by_id = {b.binding_id: b.group_name for b in self._bindings}
        _fill(
            self._messages_table,
            [
                [
                    m.state.name,
                    group_by_id.get(m.binding_id, "(deleted)"),
                    m.fetch_utc.strftime("%Y-%m-%d %H:%M:%S") if m.fetch_utc else "",
                    m.message_text,
                ]
                for m in messages
            ],
        )

    def selected_binding(self):
        row = self._bindings_table.currentRow()
        return self._bindings[row] if 0 <= row < len(self._bindings) else None

    def toggle_relay(self) -> None:
        self._controller.set_relay_enabled(not self._controller.is_relay_enabled())
        self.refresh()

    def add_binding(self, group: str, url: str, interval: int, api_key: str = "", enabled: bool = True) -> None:
        self._controller.add_or_update_binding(group, url, interval, api_key, enabled)
        self.refresh()

    def set_selected_enabled(self, enabled: bool) -> None:
        binding = self.selected_binding()
        if binding is not None:
            self._controller.set_binding_enabled(binding.binding_id, enabled)
            self.refresh()

    def remove_selected(self) -> None:
        binding = self.selected_binding()
        if binding is not None:
            self._controller.delete_binding(binding.binding_id)
            self.refresh()

    def send_test_to_selected(self, text: str) -> None:
        binding = self.selected_binding()
        if binding is not None and text.strip():
            self._controller.inject_test_message(binding.group_name, text)
            self.refresh()

    def _known_chats(self) -> Optional[list[str]]:
        lister = getattr(self._controller, "list_chats", None)
        return lister() if lister is not None else None

    def _on_add_clicked(self) -> None:
        dialog = AddBindingDialog(self, known_chats=self._known_chats())
        if dialog.exec() == AddBindingDialog.Accepted:
            values = dialog.values()
            if not values["group"]:
                QMessageBox.warning(self, "Missing group", "Enter a WhatsApp chat name.")
                return
            self.add_binding(**values)

    def _on_remove_clicked(self) -> None:
        binding = self.selected_binding()
        if binding is None:
            return
        if QMessageBox.question(self, "Remove binding", f"Remove '{binding.group_name}' and its history?") == QMessageBox.Yes:
            self.remove_selected()

    def _on_send_test_clicked(self) -> None:
        binding = self.selected_binding()
        if binding is None:
            QMessageBox.information(self, "No binding selected", "Select a binding first.")
            return
        text, ok = QInputDialog.getText(self, "Queue test message", f"Queue a test message for '{binding.group_name}':")
        if ok and text.strip():
            self.send_test_to_selected(text)


# --------------------------------------------------------------------------- #
# WhatsApp
# --------------------------------------------------------------------------- #

_CHAT_COLUMNS = ["Chat", "Unread", "Last message"]


class WhatsAppPanel(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._chats: list = []

        self._status = QLabel()
        refresh_btn = QPushButton("Refresh chats")
        refresh_btn.clicked.connect(self.refresh)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>WhatsApp</b>"))
        header.addWidget(self._status, 1)
        header.addWidget(refresh_btn)

        self._chats_table = _table(_CHAT_COLUMNS, stretch_col=2)

        bind_btn = QPushButton("Create webhook binding for chat…")
        bind_btn.clicked.connect(self._on_bind_clicked)
        send_btn = QPushButton("Send message to chat…")
        send_btn.clicked.connect(self._on_send_clicked)

        buttons = QHBoxLayout()
        buttons.addWidget(bind_btn)
        buttons.addWidget(send_btn)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._chats_table, 1)
        layout.addLayout(buttons)

    def refresh(self) -> None:
        chats = self._controller.get_whatsapp_chats()
        if chats is None:
            self._status.setText("WhatsApp integration unavailable on this platform")
            self._chats = []
            _fill(self._chats_table, [])
            return
        self._chats = list(chats)
        if not self._chats:
            self._status.setText("WhatsApp not running (or no chats visible)")
        else:
            unread = sum(1 for c in self._chats if getattr(c, "unread_count", 0) > 0)
            self._status.setText(f"{len(self._chats)} chats visible, {unread} unread")
        _fill(
            self._chats_table,
            [
                [c.chat_name, str(c.unread_count) if c.unread_count else "", getattr(c, "last_message", "")]
                for c in self._chats
            ],
        )

    def selected_chat(self):
        row = self._chats_table.currentRow()
        return self._chats[row] if 0 <= row < len(self._chats) else None

    def create_binding_for(self, group: str, url: str = "", interval: int = 3) -> None:
        self._controller.add_or_update_binding(group, url, interval)

    def send_to(self, group: str, text: str) -> tuple[bool, str]:
        return self._controller.send_to_chat(group, text)

    def _on_bind_clicked(self) -> None:
        chat = self.selected_chat()
        if chat is None:
            QMessageBox.information(self, "No chat selected", "Select a chat first.")
            return
        names = [c.chat_name for c in self._chats]
        dialog = AddBindingDialog(self, known_chats=names, initial_group=chat.chat_name)
        if dialog.exec() == AddBindingDialog.Accepted:
            values = dialog.values()
            if values["group"]:
                self._controller.add_or_update_binding(**values)
                QMessageBox.information(self, "Binding saved", f"Webhook binding for '{values['group']}' saved.")

    def _on_send_clicked(self) -> None:
        chat = self.selected_chat()
        if chat is None:
            QMessageBox.information(self, "No chat selected", "Select a chat first.")
            return
        text, ok = QInputDialog.getMultiLineText(self, "Send WhatsApp message", f"Message to '{chat.chat_name}':")
        if not (ok and text.strip()):
            return
        confirm = QMessageBox.question(
            self, "Send message", f"Send this message to '{chat.chat_name}' now?\n\n{text}"
        )
        if confirm != QMessageBox.Yes:
            return
        success, detail = self.send_to(chat.chat_name, text)
        if success:
            QMessageBox.information(self, "Sent", f"Message sent to '{chat.chat_name}'.")
        else:
            QMessageBox.warning(self, "Send failed", detail or "Unknown error.")


# --------------------------------------------------------------------------- #
# Windows (observation)
# --------------------------------------------------------------------------- #

_WINDOW_COLUMNS = ["Title", "Process", "PID", "Memory", "State", "Active"]


class WindowsPanel(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._count = QLabel()

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Open windows</b>"))
        header.addWidget(self._count, 1)

        self._table = _table(_WINDOW_COLUMNS, stretch_col=0)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._table, 1)

    def refresh(self) -> None:
        windows = self._controller.get_windows()
        self._count.setText(f"{len(windows)} windows")
        _fill(
            self._table,
            [
                [
                    w.title,
                    w.process_name,
                    str(w.process_id),
                    w.memory_display,
                    w.window_state.name.capitalize(),
                    "●" if w.is_active else "",
                ]
                for w in windows
            ],
        )


# --------------------------------------------------------------------------- #
# Events (observation)
# --------------------------------------------------------------------------- #

_EVENT_COLUMNS = ["Time (UTC)", "Event", "Process", "Window"]


class EventsPanel(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._count = QLabel()

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Recent events</b>"))
        header.addWidget(self._count, 1)

        self._table = _table(_EVENT_COLUMNS, stretch_col=3)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._table, 1)

    def refresh(self) -> None:
        events = self._controller.get_recent_events(100)
        self._count.setText(f"{len(events)} events")
        _fill(
            self._table,
            [
                [
                    e.occurred_at_utc.strftime("%H:%M:%S") if e.occurred_at_utc else "",
                    e.event_type.name,
                    e.process_name,
                    e.window_title,
                ]
                for e in events
            ],
        )
