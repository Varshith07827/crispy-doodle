"""winSpark Fetch-Webhook control panel (PySide6).

Manage WhatsApp <-> webhook bindings, flip the relay on/off, inject test
messages, and watch relayed-message history update live — a desktop front end
for the same relay the CLI and headless app drive.

The window depends only on a small duck-typed `controller` (see EngineHost in
engine_host.py for the production one). UI-logic methods (`add_binding`,
`toggle_relay`, `remove_selected`, ...) are kept separate from the modal
dialogs that call them, so they can be driven directly in headless tests.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

_BINDING_COLUMNS = ["Group", "URL", "Interval", "Enabled", "State", "Polls", "Sent"]
_MESSAGE_COLUMNS = ["State", "Group", "Fetched (UTC)", "Message"]
_REFRESH_INTERVAL_MS = 1500


class AddBindingDialog(QDialog):
    """Collects the fields for a new/updated binding. `known_chats` optionally
    pre-populates the group combo from the live WhatsApp chat list."""

    def __init__(self, parent=None, known_chats: Optional[list[str]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add / update binding")

        self._group = QComboBox()
        self._group.setEditable(True)
        if known_chats:
            self._group.addItems(known_chats)
        self._group.setCurrentText("")

        self._url = QLineEdit()
        self._url.setPlaceholderText("blank → local mock webhook for this group")

        self._interval = QSpinBox()
        self._interval.setRange(3, 3600)
        self._interval.setValue(3)
        self._interval.setSuffix(" s")

        self._api_key = QLineEdit()
        self._api_key.setPlaceholderText("optional Bearer token")

        self._enabled = QCheckBox("Enabled")
        self._enabled.setChecked(True)

        form = QFormLayout()
        form.addRow("WhatsApp chat", self._group)
        form.addRow("Webhook URL", self._url)
        form.addRow("Poll interval", self._interval)
        form.addRow("API key", self._api_key)
        form.addRow("", self._enabled)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {
            "group": self._group.currentText().strip(),
            "url": self._url.text().strip(),
            "interval": self._interval.value(),
            "api_key": self._api_key.text().strip(),
            "enabled": self._enabled.isChecked(),
        }


class MainWindow(QMainWindow):
    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._bindings: list = []

        self.setWindowTitle("winSpark — Fetch-Webhook Relay")
        self.resize(900, 640)

        self._relay_label = QLabel()
        self._relay_button = QPushButton()
        self._relay_button.clicked.connect(self.toggle_relay)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Relay</b>"))
        header.addWidget(self._relay_label, 1)
        header.addWidget(self._relay_button)

        self._bindings_table = QTableWidget(0, len(_BINDING_COLUMNS))
        self._bindings_table.setHorizontalHeaderLabels(_BINDING_COLUMNS)
        self._bindings_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._bindings_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._bindings_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._bindings_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

        add_btn = QPushButton("Add / update…")
        add_btn.clicked.connect(self._on_add_clicked)
        enable_btn = QPushButton("Enable")
        enable_btn.clicked.connect(lambda: self.set_selected_enabled(True))
        disable_btn = QPushButton("Disable")
        disable_btn.clicked.connect(lambda: self.set_selected_enabled(False))
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove_clicked)
        test_btn = QPushButton("Send test…")
        test_btn.clicked.connect(self._on_send_test_clicked)

        binding_buttons = QHBoxLayout()
        for btn in (add_btn, enable_btn, disable_btn, remove_btn, test_btn):
            binding_buttons.addWidget(btn)
        binding_buttons.addStretch(1)

        self._messages_table = QTableWidget(0, len(_MESSAGE_COLUMNS))
        self._messages_table.setHorizontalHeaderLabels(_MESSAGE_COLUMNS)
        self._messages_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._messages_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(header)
        layout.addWidget(QLabel("Bindings"))
        layout.addWidget(self._bindings_table, 2)
        layout.addLayout(binding_buttons)
        layout.addWidget(QLabel("Recent relayed messages"))
        layout.addWidget(self._messages_table, 1)
        self.setCentralWidget(central)

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh()

    # --- UI logic (directly test-driven) --------------------------------

    def refresh(self) -> None:
        enabled = self._controller.is_relay_enabled()
        self._relay_label.setText("running — polling active" if enabled else "stopped")
        self._relay_button.setText("Stop relay" if enabled else "Start relay")

        self._bindings = list(self._controller.get_bindings())
        self._bindings_table.setRowCount(len(self._bindings))
        for row, b in enumerate(self._bindings):
            values = [
                b.group_name,
                b.fetch_url,
                f"{b.poll_interval_seconds}s",
                "yes" if b.is_enabled else "no",
                b.last_fetch_state or "—",
                str(b.total_polls),
                str(b.total_sent),
            ]
            for col, value in enumerate(values):
                self._bindings_table.setItem(row, col, QTableWidgetItem(value))

        messages = self._controller.get_recent_messages(30)
        group_by_id = {b.binding_id: b.group_name for b in self._bindings}
        self._messages_table.setRowCount(len(messages))
        for row, m in enumerate(messages):
            fetched = m.fetch_utc.strftime("%Y-%m-%d %H:%M:%S") if m.fetch_utc else ""
            values = [m.state.name, group_by_id.get(m.binding_id, "(deleted)"), fetched, m.message_text]
            for col, value in enumerate(values):
                self._messages_table.setItem(row, col, QTableWidgetItem(value))

    def selected_binding(self):
        row = self._bindings_table.currentRow()
        if 0 <= row < len(self._bindings):
            return self._bindings[row]
        return None

    def toggle_relay(self) -> None:
        self._controller.set_relay_enabled(not self._controller.is_relay_enabled())
        self.refresh()

    def add_binding(self, group: str, url: str, interval: int, api_key: str = "", enabled: bool = True) -> None:
        self._controller.add_or_update_binding(group, url, interval, api_key, enabled)
        self.refresh()

    def set_selected_enabled(self, enabled: bool) -> None:
        binding = self.selected_binding()
        if binding is None:
            return
        self._controller.set_binding_enabled(binding.binding_id, enabled)
        self.refresh()

    def remove_selected(self) -> None:
        binding = self.selected_binding()
        if binding is None:
            return
        self._controller.delete_binding(binding.binding_id)
        self.refresh()

    def send_test_to_selected(self, text: str) -> None:
        binding = self.selected_binding()
        if binding is None or not text.strip():
            return
        self._controller.inject_test_message(binding.group_name, text)
        self.refresh()

    # --- modal-dialog wrappers (not unit-tested) ------------------------

    def _on_add_clicked(self) -> None:
        known = None
        lister = getattr(self._controller, "list_chats", None)
        if lister is not None:
            known = lister()
        dialog = AddBindingDialog(self, known_chats=known)
        if dialog.exec() == QDialog.Accepted:
            values = dialog.values()
            if not values["group"]:
                QMessageBox.warning(self, "Missing group", "Enter a WhatsApp chat name.")
                return
            self.add_binding(**values)

    def _on_remove_clicked(self) -> None:
        binding = self.selected_binding()
        if binding is None:
            return
        confirm = QMessageBox.question(
            self, "Remove binding", f"Remove the binding for '{binding.group_name}' and its message history?"
        )
        if confirm == QMessageBox.Yes:
            self.remove_selected()

    def _on_send_test_clicked(self) -> None:
        binding = self.selected_binding()
        if binding is None:
            QMessageBox.information(self, "No binding selected", "Select a binding first.")
            return
        text, ok = QInputDialog.getText(self, "Send test message", f"Queue a test message for '{binding.group_name}':")
        if ok and text.strip():
            self.send_test_to_selected(text)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().closeEvent(event)
