"""The per-app control panels shown on the right of the winSpark window.

`WhatsAppPanel` is the WhatsApp adapter's guided flow; `GenericAppPanel` is the
observe-only view for apps that don't have an adapter yet; `ActivityLogPanel`
shows the plain-English activity feed. All wording is deliberately non-technical.
Panel logic (choose/check/test/start/stop) is kept separate from the widgets so
it can be driven directly in headless tests.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from winspark.ui.widgets import StatusCheck, fill_table, make_table

_CHECK_INTERVALS = [
    ("Every 3 seconds", 3),
    ("Every 5 seconds", 5),
    ("Every 10 seconds", 10),
    ("Every 30 seconds", 30),
    ("Every minute", 60),
]


class WhatsAppPanel(QWidget):
    """Guided setup: choose a chat, check it, point at a message source, test it,
    pick how often to check, then start."""

    adapter_key = "whatsapp"

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._chats: list = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>WhatsApp</h2>"))
        layout.addWidget(QLabel("Automatically reply in a chat using messages from an online source."))

        # Step 1 — choose a chat
        step1 = QGroupBox("1.  Choose a chat")
        s1 = QVBoxLayout(step1)
        row1 = QHBoxLayout()
        self._chat_combo = QComboBox()
        self._chat_combo.setEditable(True)
        self._chat_combo.currentTextChanged.connect(lambda _: self._chat_check.clear_status())
        refresh_btn = QPushButton("Refresh chats")
        refresh_btn.clicked.connect(self.refresh_chats)
        check_btn = QPushButton("Check chat")
        check_btn.clicked.connect(self.check_chat)
        row1.addWidget(self._chat_combo, 1)
        row1.addWidget(refresh_btn)
        row1.addWidget(check_btn)
        s1.addLayout(row1)
        self._chat_check = StatusCheck()
        s1.addWidget(self._chat_check)
        layout.addWidget(step1)

        # Step 2 — message source
        step2 = QGroupBox("2.  Where should replies come from?")
        s2 = QVBoxLayout(step2)
        self._source = QLineEdit()
        self._source.setPlaceholderText("Paste the web address that provides replies — or leave blank to use a built-in test source")
        self._source.textChanged.connect(lambda _: self._source_check.clear_status())
        test_btn = QPushButton("Test connection")
        test_btn.clicked.connect(self.test_source)
        row2 = QHBoxLayout()
        row2.addWidget(self._source, 1)
        row2.addWidget(test_btn)
        s2.addLayout(row2)
        self._source_check = StatusCheck()
        s2.addWidget(self._source_check)
        layout.addWidget(step2)

        # Step 3 — how often
        step3 = QGroupBox("3.  How often should we check?")
        s3 = QHBoxLayout(step3)
        self._interval_combo = QComboBox()
        for label, seconds in _CHECK_INTERVALS:
            self._interval_combo.addItem(label, seconds)
        s3.addWidget(self._interval_combo)
        s3.addStretch(1)
        layout.addWidget(step3)

        # Step 4 — start / stop
        step4 = QGroupBox("4.  Turn it on")
        s4 = QHBoxLayout(step4)
        self._start_button = QPushButton("Start automation")
        self._start_button.clicked.connect(self.toggle_automation)
        self._run_status = QLabel()
        s4.addWidget(self._start_button)
        s4.addWidget(self._run_status, 1)
        layout.addWidget(step4)

        layout.addStretch(1)
        self.refresh_chats()

    # --- logic (test-driven) -------------------------------------------

    def refresh_chats(self) -> None:
        chats = self._controller.get_whatsapp_chats()
        current = self._chat_combo.currentText()
        self._chat_combo.blockSignals(True)
        self._chat_combo.clear()
        if chats is None:
            self._chats = []
            self._chat_combo.setEnabled(False)
            self._chat_combo.setEditText("")
        else:
            self._chats = list(chats)
            self._chat_combo.setEnabled(True)
            self._chat_combo.addItems([c.chat_name for c in self._chats])
            self._chat_combo.setEditText(current)
        self._chat_combo.blockSignals(False)
        self.refresh()

    def current_chat(self) -> str:
        return self._chat_combo.currentText().strip()

    def selected_interval(self) -> int:
        return self._interval_combo.currentData()

    def check_chat(self) -> None:
        chat = self.current_chat()
        if not chat:
            self._chat_check.set_bad("Choose a chat first")
            return
        self._chat_check.set_busy("Looking for the chat…")
        if self._controller.can_find_chat(chat):
            self._chat_check.set_ok("Found this chat")
        else:
            self._chat_check.set_bad("Couldn't find this chat — open it in WhatsApp, then Refresh")

    def test_source(self) -> None:
        chat = self.current_chat() or "chat"
        self._source_check.set_busy("Testing connection…")
        ok, detail = self._controller.test_message_source(self._source.text().strip(), chat)
        if ok:
            self._source_check.set_ok("Connected")
        else:
            self._source_check.set_bad(detail)

    def is_running(self) -> bool:
        chat = self.current_chat()
        return bool(chat) and self._controller.is_chat_automation_running(chat)

    def toggle_automation(self) -> None:
        chat = self.current_chat()
        if not chat:
            self._chat_check.set_bad("Choose a chat first")
            return
        if self.is_running():
            self._controller.stop_chat_automation(chat)
        else:
            self._controller.start_chat_automation(chat, self._source.text().strip(), self.selected_interval())
        self.refresh()

    def refresh(self) -> None:
        running = self.is_running()
        self._start_button.setText("Stop automation" if running else "Start automation")
        chat = self.current_chat()
        if not chat:
            self._run_status.setText("Choose a chat to begin.")
        elif running:
            self._run_status.setText(f"On — replying in “{chat}”.")
        else:
            self._run_status.setText("Off.")


class GenericAppPanel(QWidget):
    """Shown for a running app that has no automation adapter yet."""

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        layout = QVBoxLayout(self)
        self._title = QLabel()
        self._body = QLabel()
        self._body.setWordWrap(True)
        layout.addWidget(self._title)
        layout.addWidget(self._body)
        layout.addStretch(1)

    def set_app(self, app) -> None:
        self._title.setText(f"<h2>{app.display_name}</h2>")
        windows = "1 window" if app.window_count == 1 else f"{app.window_count} windows"
        self._body.setText(
            f"winSpark can see {app.display_name} ({windows} open), but can't automate it yet.\n\n"
            "Automation is available for WhatsApp today. Support for more apps is on the way."
        )


class ActivityLogPanel(QWidget):
    """The plain-English activity feed."""

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("<b>Activity</b>"))
        self._table = make_table(["When", "What happened"], stretch_col=1)
        layout.addWidget(self._table)

    def refresh(self) -> None:
        entries = self._controller.get_activity_log(200)
        fill_table(
            self._table,
            [[when.strftime("%H:%M:%S"), text] for when, text in entries],
        )
