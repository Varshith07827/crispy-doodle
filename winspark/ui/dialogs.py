"""Modal dialogs for the winSpark UI. Kept separate from the panels so panel
logic (which is unit-tested headless) doesn't pull in modal interaction."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)


class AddBindingDialog(QDialog):
    """Collects the fields for a new/updated fetch-webhook binding.
    `known_chats` optionally pre-populates the group combo from the live
    WhatsApp chat list; `initial_group` pre-selects one (e.g. when launched
    from the WhatsApp panel with a chat already chosen)."""

    def __init__(self, parent=None, known_chats: Optional[list[str]] = None, initial_group: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Add / update binding")

        self._group = QComboBox()
        self._group.setEditable(True)
        if known_chats:
            self._group.addItems(known_chats)
        self._group.setCurrentText(initial_group)

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
