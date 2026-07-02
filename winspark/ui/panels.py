"""The per-app control panels shown on the right of the winSpark window.

`WhatsAppPanel` is the WhatsApp adapter's guided flow; `GenericAppPanel` is the
observe-only view for apps that don't have an adapter yet; `ActivityLogPanel`
shows the plain-English activity feed. All wording is deliberately non-technical.
Panel logic (choose/check/test/start/stop) is kept separate from the widgets so
it can be driven directly in headless tests.
"""

from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

_MESSAGE_POLL_INTERVAL_MS = 3000
_RECENT_MESSAGE_LIMIT = 15

from winspark.constants import ai_provider_info
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

    # Results from background work, delivered back on the UI thread. Every call
    # that drives WhatsApp (reading messages, sending, opening a chat) hits the
    # STA thread and can block for up to 30s, so it must NOT run on the UI thread
    # — that was the periodic freeze. Workers emit these; the slots update the UI.
    _messages_ready = Signal(object, object)
    _send_done = Signal(bool, str)
    _open_done = Signal(bool, str)

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._chats: list = []
        self._msg_busy = False
        self._action_busy = False
        # Overridable so tests can run "background" work inline/synchronously.
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        self._messages_ready.connect(self._on_messages_ready)
        self._send_done.connect(self._on_send_done)
        self._open_done.connect(self._on_open_done)

        # The guided flow is taller than most windows, so put it in a scroll area
        # — otherwise the lower steps (Messages, Start) get cut off with no way
        # to reach them.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        layout = QVBoxLayout(content)
        layout.addWidget(QLabel("<h2>WhatsApp</h2>"))
        layout.addWidget(QLabel("Automatically reply in a chat using messages from an online source."))

        # Step 1 — choose a chat
        step1 = QGroupBox("1.  Choose a chat")
        s1 = QVBoxLayout(step1)
        row1 = QHBoxLayout()
        self._chat_combo = QComboBox()
        self._chat_combo.setEditable(True)
        self._chat_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._chat_combo.lineEdit().setPlaceholderText("Pick a recent chat, or type any chat name…")
        self._chat_combo.currentTextChanged.connect(lambda _: self._chat_check.clear_status())
        refresh_btn = QPushButton("Refresh chats")
        refresh_btn.clicked.connect(self.refresh_chats)
        check_btn = QPushButton("Check chat")
        check_btn.clicked.connect(self.check_chat)
        row1.addWidget(self._chat_combo, 1)
        row1.addWidget(refresh_btn)
        row1.addWidget(check_btn)
        s1.addLayout(row1)
        hint = QLabel("Don't see your chat in the list? Type its name above and press Check chat — we'll search WhatsApp for it.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        s1.addWidget(hint)
        self._chat_check = StatusCheck()
        s1.addWidget(self._chat_check)
        layout.addWidget(step1)

        # Step 2 — where replies come from (a web/test source, or OpenAI)
        step2 = QGroupBox("2.  Where should replies come from?")
        s2 = QVBoxLayout(step2)

        self._method_combo = QComboBox()
        self._method_combo.addItem("A web address, or the built-in test source", "web")
        self._method_combo.addItem("AI (OpenAI or Groq) — let AI write the replies", "openai")
        self._method_combo.addItem("Watch for a message and reply", "trigger")
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        s2.addWidget(self._method_combo)

        # Web / built-in test source sub-panel
        self._web_panel = QWidget()
        web = QVBoxLayout(self._web_panel)
        web.setContentsMargins(0, 0, 0, 0)
        self._source = QLineEdit()
        self._source.setPlaceholderText("Paste the web address that provides replies — or leave blank to use a built-in test source")
        self._source.textChanged.connect(lambda _: self._source_check.clear_status())
        web_test = QPushButton("Test connection")
        web_test.clicked.connect(self.test_source)
        web_row = QHBoxLayout()
        web_row.addWidget(self._source, 1)
        web_row.addWidget(web_test)
        web.addLayout(web_row)
        s2.addWidget(self._web_panel)

        # AI sub-panel — the provider/key/model are app-wide; the prompt is per chat.
        self._ai_panel = QWidget()
        ai = QVBoxLayout(self._ai_panel)
        ai.setContentsMargins(0, 0, 0, 0)
        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("AI service:"))
        self._ai_provider = QComboBox()
        self._ai_provider.addItem("OpenAI", "openai")
        self._ai_provider.addItem("Groq", "groq")
        self._ai_provider.setCurrentIndex(max(0, self._ai_provider.findData(self._controller.get_ai_provider())))
        self._ai_provider.currentIndexChanged.connect(self._on_ai_provider_changed)
        provider_row.addWidget(self._ai_provider, 1)
        ai.addLayout(provider_row)
        self._ai_key = QLineEdit()
        self._ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._ai_key.setPlaceholderText("Your API key (saved once, shared by every chat)")
        self._ai_key.setText(self._controller.get_openai_api_key())
        self._ai_key.textChanged.connect(lambda _: self._source_check.clear_status())
        self._ai_model = QLineEdit()
        self._ai_model.setText(self._controller.get_openai_model())
        ai_test = QPushButton("Test connection")
        ai_test.clicked.connect(self.test_source)
        key_row = QHBoxLayout()
        key_row.addWidget(self._ai_key, 2)
        key_row.addWidget(self._ai_model, 1)
        key_row.addWidget(ai_test)
        ai.addLayout(key_row)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("When to reply:"))
        self._ai_mode = QComboBox()
        self._ai_mode.addItem("Reply to each new message", "reply")
        self._ai_mode.addItem("Post a new message every check", "generate")
        self._ai_mode.currentIndexChanged.connect(self._on_ai_mode_changed)
        mode_row.addWidget(self._ai_mode, 1)
        ai.addLayout(mode_row)
        ai.addWidget(QLabel("How should the AI write? (your instructions)"))
        self._ai_prompt = QPlainTextEdit()
        self._ai_prompt.setPlaceholderText("e.g. Reply warmly and briefly, as my friendly personal assistant.")
        self._ai_prompt.setFixedHeight(60)
        ai.addWidget(self._ai_prompt)
        self._ai_mode_hint = QLabel()
        self._ai_mode_hint.setWordWrap(True)
        self._ai_mode_hint.setStyleSheet("color: gray;")
        ai.addWidget(self._ai_mode_hint)
        s2.addWidget(self._ai_panel)

        # Watch-for-a-message sub-panel: wait for a phrase, reply with a set text.
        self._trigger_panel = QWidget()
        tg = QVBoxLayout(self._trigger_panel)
        tg.setContentsMargins(0, 0, 0, 0)
        tg.addWidget(QLabel("Wait for a message that means…"))
        self._trigger_text = QLineEdit()
        self._trigger_text.setPlaceholderText("e.g. asking if I'm coming, or the word \"invoice\"")
        tg.addWidget(self._trigger_text)
        tg.addWidget(QLabel("…then automatically reply with:"))
        self._trigger_reply = QPlainTextEdit()
        self._trigger_reply.setPlaceholderText("e.g. Yes, I'll be there! See you soon.")
        self._trigger_reply.setFixedHeight(56)
        tg.addWidget(self._trigger_reply)
        trigger_hint = QLabel(
            "When a new message arrives that matches, winSpark sends your reply once. "
            "With OpenAI set up it matches by meaning; otherwise it matches the words."
        )
        trigger_hint.setWordWrap(True)
        trigger_hint.setStyleSheet("color: gray;")
        tg.addWidget(trigger_hint)
        s2.addWidget(self._trigger_panel)

        self._source_check = StatusCheck()
        s2.addWidget(self._source_check)
        layout.addWidget(step2)
        self._on_method_changed()
        self._on_ai_mode_changed()
        self._on_ai_provider_changed()

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

        # Messages — a live view of the open chat + a box to send one yourself.
        convo = QGroupBox("Messages")
        cv = QVBoxLayout(convo)
        self._messages_view = QPlainTextEdit()
        self._messages_view.setReadOnly(True)
        self._messages_view.setPlaceholderText("Recent messages from the chat open in WhatsApp will appear here.")
        self._messages_view.setFixedHeight(150)
        cv.addWidget(self._messages_view)
        self._messages_status = QLabel()
        self._messages_status.setStyleSheet("color: gray;")
        cv.addWidget(self._messages_status)
        send_row = QHBoxLayout()
        self._compose = QLineEdit()
        self._compose.setPlaceholderText("Type a message to send to this chat…")
        self._compose.returnPressed.connect(self.send_message)
        send_btn = QPushButton("Send")
        send_btn.clicked.connect(self.send_message)
        open_btn = QPushButton("Open chat")
        open_btn.clicked.connect(self.open_chat)
        send_row.addWidget(self._compose, 1)
        send_row.addWidget(send_btn)
        send_row.addWidget(open_btn)
        cv.addLayout(send_row)
        self._send_check = StatusCheck()
        cv.addWidget(self._send_check)
        layout.addWidget(convo)

        layout.addStretch(1)

        # Poll recent messages every few seconds, but only while this panel is on
        # screen (started/stopped in show/hideEvent) so we don't read WhatsApp in
        # the background. The read itself doesn't open or foreground anything.
        self._msg_timer = QTimer(self)
        self._msg_timer.setInterval(_MESSAGE_POLL_INTERVAL_MS)
        self._msg_timer.timeout.connect(self.refresh_messages)

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
            self._chat_check.set_bad("Couldn't find this chat — check the name, or open it once in WhatsApp")

    def current_reply_source(self) -> str:
        return self._method_combo.currentData()

    def _on_method_changed(self, *_args) -> None:
        source = self.current_reply_source()
        self._web_panel.setVisible(source == "web")
        self._ai_panel.setVisible(source == "openai")
        self._trigger_panel.setVisible(source == "trigger")
        self._source_check.clear_status()

    def current_ai_mode(self) -> str:
        return self._ai_mode.currentData()

    def current_ai_provider(self) -> str:
        return self._ai_provider.currentData()

    def _on_ai_provider_changed(self, *_args) -> None:
        default_model = ai_provider_info(self.current_ai_provider())["default_model"]
        self._ai_model.setPlaceholderText(f"Model (blank = {default_model})")
        self._source_check.clear_status()

    def _on_ai_mode_changed(self, *_args) -> None:
        if self.current_ai_mode() == "reply":
            self._ai_mode_hint.setText(
                "When someone messages this chat, winSpark reads it and replies with AI. "
                "It won't reply to your own messages, and answers each message once."
            )
        else:
            self._ai_mode_hint.setText("winSpark posts a fresh AI-written message on every check.")

    def test_source(self) -> None:
        chat = self.current_chat() or "chat"
        self._source_check.set_busy("Testing connection…")
        if self.current_reply_source() == "openai":
            self._controller.set_openai_config(
                self._ai_key.text().strip(), self._ai_model.text().strip(), self.current_ai_provider()
            )
            ok, detail = self._controller.test_openai_connection()
        else:
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
        source = self.current_reply_source()
        if self.is_running():
            self._controller.stop_chat_automation(chat)
        elif source == "openai":
            self._controller.set_openai_config(
                self._ai_key.text().strip(), self._ai_model.text().strip(), self.current_ai_provider()
            )
            self._controller.start_chat_automation(
                chat, "", self.selected_interval(),
                reply_source="openai", ai_mode=self.current_ai_mode(), ai_prompt=self._ai_prompt.toPlainText().strip(),
            )
        elif source == "trigger":
            self._controller.start_chat_automation(
                chat, "", self.selected_interval(),
                reply_source="trigger",
                trigger_text=self._trigger_text.text().strip(),
                reply_text=self._trigger_reply.toPlainText().strip(),
            )
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

    # --- messages (send + live view) -----------------------------------

    def send_message(self) -> None:
        chat = self.current_chat()
        if not chat:
            self._send_check.set_bad("Choose a chat first")
            return
        text = self._compose.text().strip()
        if not text or self._action_busy:
            return
        self._action_busy = True
        self._send_check.set_busy("Sending…")

        def worker():
            try:
                ok, detail = self._controller.send_to_chat(chat, text)
            except Exception as ex:  # noqa: BLE001
                ok, detail = False, str(ex)
            self._send_done.emit(bool(ok), detail or "")

        self._spawn(worker)

    def _on_send_done(self, ok: bool, detail: str) -> None:
        self._action_busy = False
        if ok:
            self._compose.clear()
            self._send_check.set_ok("Sent")
            self.refresh_messages()
        else:
            self._send_check.set_bad(detail)

    def open_chat(self) -> None:
        chat = self.current_chat()
        if not chat:
            self._send_check.set_bad("Choose a chat first")
            return
        if self._action_busy:
            return
        self._action_busy = True
        self._send_check.set_busy(f"Opening {chat}…")

        def worker():
            try:
                ok = self._controller.open_chat(chat)
            except Exception:  # noqa: BLE001
                ok = False
            self._open_done.emit(bool(ok), "")

        self._spawn(worker)

    def _on_open_done(self, ok: bool, _detail: str) -> None:
        self._action_busy = False
        if ok:
            self._send_check.clear_status()
            self.refresh_messages()
        else:
            self._send_check.set_bad("Couldn't open this chat")

    def refresh_messages(self) -> None:
        # Reading messages hits the STA thread (slow); run it off the UI thread so
        # the 3-second poll never freezes the window. Skip if a read's in flight.
        if self._msg_busy:
            return
        self._msg_busy = True

        def worker():
            try:
                active, messages = self._controller.get_recent_messages(_RECENT_MESSAGE_LIMIT)
            except Exception:  # noqa: BLE001
                active, messages = None, []
            self._messages_ready.emit(active, messages or [])

        self._spawn(worker)

    def _on_messages_ready(self, active, messages) -> None:
        self._msg_busy = False
        self._render_messages(active, messages)

    def _render_messages(self, active, messages) -> None:
        if not messages:
            self._messages_view.setPlainText("")
            self._messages_status.setText(
                "No messages yet — press “Open chat” to load this chat in WhatsApp."
                if self.current_chat()
                else ""
            )
            return
        lines = [f"{'You' if not m.is_incoming else (m.sender or 'Them')}:  {m.text}" for m in messages]
        self._messages_view.setPlainText("\n".join(lines))
        scrollbar = self._messages_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        if active:
            self._messages_status.setText(f"Showing “{active}” — updates every 3 seconds")
        else:
            self._messages_status.setText("Showing the chat currently open in WhatsApp")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._msg_timer.start()
        self.refresh_messages()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._msg_timer.stop()


class GenericAppPanel(QWidget):
    """Shown for a running app that has no automation adapter yet. winSpark can't
    drive it, but it CAN read the text on its screen with Windows OCR — useful
    for pulling info out of apps it doesn't understand natively."""

    _ocr_done = Signal(bool, str)

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._app = None
        self._ocr_busy = False
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        self._ocr_done.connect(self._on_ocr_done)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        layout = QVBoxLayout(content)
        self._title = QLabel()
        self._body = QLabel()
        self._body.setWordWrap(True)
        layout.addWidget(self._title)
        layout.addWidget(self._body)

        read_group = QGroupBox("Read text on screen")
        rg = QVBoxLayout(read_group)
        rg.addWidget(QLabel("winSpark can read the text shown in this app's window using Windows OCR."))
        button_row = QHBoxLayout()
        self._read_btn = QPushButton("Read text on screen")
        self._read_btn.clicked.connect(self.read_text)
        self._copy_btn = QPushButton("Copy text")
        self._copy_btn.clicked.connect(self.copy_text)
        self._copy_btn.setEnabled(False)
        button_row.addWidget(self._read_btn)
        button_row.addWidget(self._copy_btn)
        button_row.addStretch(1)
        rg.addLayout(button_row)
        self._ocr_view = QPlainTextEdit()
        self._ocr_view.setReadOnly(True)
        self._ocr_view.setPlaceholderText("Press “Read text on screen” to capture what this app is showing.")
        rg.addWidget(self._ocr_view)
        self._ocr_status = QLabel()
        self._ocr_status.setStyleSheet("color: gray;")
        rg.addWidget(self._ocr_status)
        layout.addWidget(read_group)
        layout.addStretch(1)

    def set_app(self, app) -> None:
        self._app = app
        self._title.setText(f"<h2>{app.display_name}</h2>")
        windows = "1 window" if app.window_count == 1 else f"{app.window_count} windows"
        self._body.setText(
            f"winSpark can see {app.display_name} ({windows} open). It can't automate this app yet, "
            "but it can read the text on its screen below."
        )
        self._ocr_view.clear()
        self._ocr_status.clear()
        self._copy_btn.setEnabled(False)

    def _primary_handle(self) -> Optional[int]:
        if self._app is None or not self._app.window_handles:
            return None
        return self._app.window_handles[0]

    def read_text(self) -> None:
        handle = self._primary_handle()
        if handle is None:
            self._ocr_status.setText("No window to read.")
            return
        if self._ocr_busy:
            return
        self._ocr_busy = True
        self._read_btn.setEnabled(False)
        self._ocr_status.setText("Reading the screen…")

        def worker():
            try:
                ok, result = self._controller.read_screen_text(handle)
            except Exception as ex:  # noqa: BLE001
                ok, result = False, str(ex)
            self._ocr_done.emit(bool(ok), result or "")

        self._spawn(worker)

    def _on_ocr_done(self, ok: bool, result: str) -> None:
        self._ocr_busy = False
        self._read_btn.setEnabled(True)
        if ok:
            self._ocr_view.setPlainText(result)
            self._copy_btn.setEnabled(True)
            self._ocr_status.setText("Read the text below — press “Copy text” to use it elsewhere.")
        else:
            self._ocr_view.clear()
            self._copy_btn.setEnabled(False)
            self._ocr_status.setText(result)

    def copy_text(self) -> None:
        text = self._ocr_view.toPlainText()
        if not text:
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        self._ocr_status.setText("Copied to the clipboard.")


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
