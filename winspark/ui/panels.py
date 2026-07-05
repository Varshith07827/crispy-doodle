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

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

_MESSAGE_POLL_INTERVAL_MS = 3000
_RECENT_MESSAGE_LIMIT = 15


def _describe_binding_method(binding) -> str:
    """Plain-English summary of what a saved automation does, for the
    "Your automations" list."""
    if binding.reply_source == "trigger":
        phrase = binding.trigger_text.strip() or "…"
        return f'Watch for "{phrase}"'
    if binding.reply_source == "openai":
        return "AI reply to messages" if binding.ai_mode == "reply" else "AI posts on a schedule"
    return "Web address"


class _NoWheelComboBox(QComboBox):
    """A dropdown that ignores the mouse wheel. The panels live in scroll
    areas, so with Qt's default behavior, scrolling the page while the cursor
    happens to pass over a dropdown silently changes the chosen option instead
    of scrolling — easy to do by accident and easy to miss having done.
    Ignoring the wheel event lets it bubble up to the scroll area, so the page
    scrolls as expected; changing the option takes a deliberate click."""

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        event.ignore()


def _allow_narrow(combo: QComboBox) -> None:
    """Let a combo shrink below its longest item's width (long chat names or
    method labels would otherwise force the whole panel wider than the window;
    the popup list still shows items in full)."""
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(18)

from winspark.constants import ai_provider_info
from winspark.ui.engine_host import (
    AUTOMATION_APP_ACTION,
    AUTOMATION_WHATSAPP,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    TRIGGER_SCREEN,
)
from winspark.ui.widgets import StatusCheck, fill_table, make_table

_WATCH_INTERVALS = [
    ("Every 10 seconds", 10),
    ("Every 30 seconds", 30),
    ("Every minute", 60),
    ("Every 5 minutes", 300),
]

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
    _chats_ready = Signal(object)

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._chats: list = []
        self._msg_busy = False
        self._action_busy = False
        self._chats_busy = False
        # Overridable so tests can run "background" work inline/synchronously.
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        self._messages_ready.connect(self._on_messages_ready)
        self._send_done.connect(self._on_send_done)
        self._open_done.connect(self._on_open_done)
        self._chats_ready.connect(self._on_chats_ready)

        # The guided flow is taller than most windows, so put it in a scroll area
        # — otherwise the lower steps (Messages, Start) get cut off with no way
        # to reach them.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        # The layout must exist BEFORE setWidget, or the scroll area can't
        # constrain the content's width (Qt docs; without it the panel renders
        # wider than the window and gets clipped).
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        layout.addWidget(QLabel("<h2>WhatsApp</h2>"))
        intro = QLabel("Reply in a chat automatically, using replies from an online source or written by AI.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Step 1 — choose a chat. Ported from the original .NET app's design
        # (WhatsAppCommandPanel.xaml / MainViewModel.WhatsAppAutomation.cs)
        # rather than an editable combo box: an always-visible QListWidget of
        # recent chats (no popup to open, so nothing to fail to open) plus a
        # separate plain text field for the chat name. Clicking a list item
        # just copies its name into the field — the same
        # OnSelectedWhatsAppChatChanged -> NewFetchGroupName = value.ChatName
        # wiring the original used.
        step1 = QGroupBox("1.  Choose a chat")
        s1 = QVBoxLayout(step1)
        row1 = QHBoxLayout()
        refresh_btn = QPushButton("Refresh chats")
        refresh_btn.clicked.connect(self.refresh_chats)
        check_btn = QPushButton("Check chat")
        check_btn.clicked.connect(self.check_chat)
        row1.addWidget(refresh_btn)
        row1.addWidget(check_btn)
        row1.addStretch(1)
        s1.addLayout(row1)
        self._chat_list = QListWidget()
        self._chat_list.setMaximumHeight(140)
        self._chat_list.itemClicked.connect(self._on_chat_list_item_clicked)
        s1.addWidget(self._chat_list)
        self._chat_name = QLineEdit()
        self._chat_name.setPlaceholderText("Type the exact chat name as shown in WhatsApp, or pick one above")
        self._chat_name.textChanged.connect(lambda _: self._chat_check.clear_status())
        s1.addWidget(self._chat_name)
        hint = QLabel("Don't see your chat in the list? Type its name above and press Check chat — we'll search WhatsApp for it.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #64748b;")
        s1.addWidget(hint)
        self._chat_check = StatusCheck()
        s1.addWidget(self._chat_check)
        layout.addWidget(step1)

        # Step 2 — where replies come from (a web/test source, or OpenAI)
        step2 = QGroupBox("2.  Where should replies come from?")
        s2 = QVBoxLayout(step2)

        self._method_combo = _NoWheelComboBox()
        _allow_narrow(self._method_combo)
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
        web.addWidget(self._source)
        web_test = QPushButton("Test connection")
        web_test.clicked.connect(self.test_source)
        web_row = QHBoxLayout()
        web_row.addWidget(web_test)
        web_row.addStretch(1)
        web.addLayout(web_row)
        s2.addWidget(self._web_panel)

        # AI sub-panel — mode + prompt only. The AI service itself (provider,
        # key, model) is app-wide and lives in Settings; duplicating those
        # fields here made an app-wide setting look WhatsApp-specific.
        self._ai_panel = QWidget()
        ai = QVBoxLayout(self._ai_panel)
        ai.setContentsMargins(0, 0, 0, 0)
        ai_where = QLabel("Uses the AI service from ⚙ Settings (bottom left).")
        ai_where.setWordWrap(True)
        ai_where.setStyleSheet("color: #64748b;")
        ai.addWidget(ai_where)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("When to reply:"))
        self._ai_mode = _NoWheelComboBox()
        _allow_narrow(self._ai_mode)
        self._ai_mode.addItem("Reply to each new message", "reply")
        self._ai_mode.addItem("Post a new message every check", "generate")
        self._ai_mode.currentIndexChanged.connect(self._on_ai_mode_changed)
        mode_row.addWidget(self._ai_mode, 1)
        ai.addLayout(mode_row)
        ai_prompt_label = QLabel("How should the AI write? (your instructions)")
        ai_prompt_label.setWordWrap(True)
        ai.addWidget(ai_prompt_label)
        self._ai_prompt = QPlainTextEdit()
        self._ai_prompt.setPlaceholderText("e.g. Reply warmly and briefly, as my friendly personal assistant.")
        self._ai_prompt.setFixedHeight(60)
        ai.addWidget(self._ai_prompt)
        self._ai_mode_hint = QLabel()
        self._ai_mode_hint.setWordWrap(True)
        self._ai_mode_hint.setStyleSheet("color: #64748b;")
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
        trigger_hint.setStyleSheet("color: #64748b;")
        tg.addWidget(trigger_hint)
        s2.addWidget(self._trigger_panel)

        self._source_check = StatusCheck()
        s2.addWidget(self._source_check)
        layout.addWidget(step2)
        self._on_method_changed()
        self._on_ai_mode_changed()

        # Step 3 — how often
        step3 = QGroupBox("3.  How often should we check?")
        s3 = QHBoxLayout(step3)
        self._interval_combo = _NoWheelComboBox()
        for label, seconds in _CHECK_INTERVALS:
            self._interval_combo.addItem(label, seconds)
        s3.addWidget(self._interval_combo)
        s3.addStretch(1)
        layout.addWidget(step3)

        # Step 4 — start / stop
        step4 = QGroupBox("4.  Turn it on")
        s4 = QHBoxLayout(step4)
        self._start_button = QPushButton("Start automation")
        self._start_button.setObjectName("primary")
        self._start_button.clicked.connect(self.toggle_automation)
        self._run_status = QLabel()
        s4.addWidget(self._start_button)
        s4.addWidget(self._run_status, 1)
        layout.addWidget(step4)

        # Your automations — every chat that has automation configured, not just
        # the one currently selected above, so you can see what's running and
        # pause/stop ("disband") anything you don't want running anymore.
        auto_group = QGroupBox("Your automations")
        ag2 = QVBoxLayout(auto_group)
        auto_hint = QLabel("All chats with automation — pause or remove them here.")
        auto_hint.setWordWrap(True)
        ag2.addWidget(auto_hint)
        self._automations_table = make_table(["Chat", "What it does", "Status", ""], stretch_col=1)
        self._automations_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._automations_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._automations_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._automations_table.setMinimumHeight(96)
        self._automations_table.setMaximumHeight(160)
        ag2.addWidget(self._automations_table)
        self._automations_empty_label = QLabel("No automations set up yet — start one above.")
        self._automations_empty_label.setStyleSheet("color: #64748b;")
        ag2.addWidget(self._automations_empty_label)
        layout.addWidget(auto_group)

        # Messages — a live view of the open chat + a box to send one yourself.
        convo = QGroupBox("Messages")
        cv = QVBoxLayout(convo)
        self._messages_view = QPlainTextEdit()
        self._messages_view.setReadOnly(True)
        self._messages_view.setPlaceholderText("Recent messages from the chat open in WhatsApp will appear here.")
        self._messages_view.setFixedHeight(120)
        cv.addWidget(self._messages_view)
        self._messages_status = QLabel()
        self._messages_status.setStyleSheet("color: #64748b;")
        cv.addWidget(self._messages_status)
        self._compose = QLineEdit()
        self._compose.setPlaceholderText("Type a message to send to this chat…")
        self._compose.returnPressed.connect(self.send_message)
        cv.addWidget(self._compose)
        send_row = QHBoxLayout()
        send_btn = QPushButton("Send")
        send_btn.setObjectName("primary")
        send_btn.clicked.connect(self.send_message)
        open_btn = QPushButton("Open chat")
        open_btn.clicked.connect(self.open_chat)
        send_row.addWidget(send_btn)
        send_row.addWidget(open_btn)
        send_row.addStretch(1)
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

        # Deliberately NOT loading chats here: that's an STA WhatsApp read that
        # takes seconds, and this constructor runs before the app window can
        # appear. The main window triggers refresh_chats() when this panel is
        # actually selected.
        self.refresh_automations()
        self.refresh()

    # --- logic (test-driven) -------------------------------------------

    def refresh_chats(self) -> None:
        """Reload the recent-chats list. Ported from RefreshWhatsAppChatsAsync
        in the original .NET app: repopulate an always-visible list, don't
        touch whatever the user has already typed into the name field.

        Runs on a worker thread: this is a full STA read of WhatsApp's chat
        list (seconds). It used to run synchronously in the panel constructor,
        which alone kept the whole app window from appearing for several
        seconds at launch — now nothing reads WhatsApp until this panel is
        actually selected, and even then the UI stays live while it loads."""
        if self._chats_busy:
            return
        self._chats_busy = True
        self._chat_check.set_busy("Loading your chats…")

        def worker():
            try:
                chats = self._controller.get_whatsapp_chats()
            except Exception:  # noqa: BLE001
                chats = None
            self._chats_ready.emit(chats)

        self._spawn(worker)

    def _on_chats_ready(self, chats) -> None:
        self._chats_busy = False
        self._chat_check.clear_status()
        self._chat_list.clear()
        if chats is None:
            self._chats = []
            self._chat_list.setEnabled(False)
        else:
            self._chats = list(chats)
            self._chat_list.setEnabled(True)
            self._chat_list.addItems([c.chat_name for c in self._chats])
        self.refresh()

    def _on_chat_list_item_clicked(self, item) -> None:
        """Ported from OnSelectedWhatsAppChatChanged in the original .NET app:
        picking a chat from the list just copies its name into the text
        field — no combo box, no popup, nothing that can fail to open."""
        self._chat_name.setText(item.text())
        self._chat_check.clear_status()

    def current_chat(self) -> str:
        return self._chat_name.text().strip()

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
        self.refresh_automations()

    # --- automations list (see what's running, pause/remove) ------------

    def refresh_automations(self) -> None:
        """Re-list every chat with automation configured. A plain DB read (no
        WhatsApp UI Automation involved), so this is cheap enough to call on
        every periodic refresh tick, unlike the chat list / message view."""
        bindings = self._controller.get_bindings()
        table = self._automations_table
        table.setRowCount(len(bindings))
        self._automations_empty_label.setVisible(not bindings)
        table.setVisible(bool(bindings))

        for row, binding in enumerate(bindings):
            table.setItem(row, 0, QTableWidgetItem(binding.group_name))
            table.setItem(row, 1, QTableWidgetItem(_describe_binding_method(binding)))
            status_item = QTableWidgetItem("Running" if binding.is_enabled else "Paused")
            table.setItem(row, 2, status_item)

            actions = QWidget()
            actions_row = QHBoxLayout(actions)
            actions_row.setContentsMargins(2, 0, 2, 0)
            toggle_btn = QPushButton("Pause" if binding.is_enabled else "Resume")
            toggle_btn.clicked.connect(lambda _checked=False, b=binding: self.toggle_binding(b))
            remove_btn = QPushButton("Remove")
            remove_btn.clicked.connect(lambda _checked=False, b=binding: self.remove_binding(b))
            actions_row.addWidget(toggle_btn)
            actions_row.addWidget(remove_btn)
            table.setCellWidget(row, 3, actions)

    def toggle_binding(self, binding) -> None:
        self._controller.set_binding_enabled(binding.binding_id, not binding.is_enabled)
        self.refresh_automations()
        self.refresh()

    def remove_binding(self, binding) -> None:
        confirm = QMessageBox.question(
            self,
            "Remove automation",
            f"Stop and remove the automation for “{binding.group_name}”?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._controller.delete_binding(binding.binding_id)
        self.refresh_automations()
        self.refresh()

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

    _ocr_done = Signal(bool, str, object)  # (ok, text-or-error, png bytes or None)
    _ask_done = Signal(bool, str)
    _agent_progress = Signal(str)          # one line to append to the step log
    _agent_ask = Signal(str)               # a risky step needs approval (description)
    _agent_finished = Signal(bool, str)    # loop over: (ok, summary-or-error)

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._app = None
        self._ocr_busy = False
        self._ask_busy = False
        self._agent_busy = False
        # Cross-thread approval handshake for risky steps: the loop worker
        # blocks on the event while the user decides. Overridable in tests
        # (and replaceable if a different approval UI is ever wanted).
        self._approval_event = threading.Event()
        self._approval_decision = False
        self._request_approval = self._request_approval_via_ui
        # Set by the Stop button; the loop checks it between rounds (a step
        # already in flight finishes — we never yank input mid-keystroke).
        self._agent_stop = threading.Event()
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        self._ocr_done.connect(self._on_ocr_done)
        self._ask_done.connect(self._on_ask_done)
        self._agent_progress.connect(self._on_agent_progress)
        self._agent_ask.connect(self._on_agent_ask)
        self._agent_finished.connect(self._on_agent_finished)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)  # must exist before setWidget (see WhatsAppPanel)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        self._title = QLabel()
        self._body = QLabel()
        self._body.setWordWrap(True)
        layout.addWidget(self._title)
        layout.addWidget(self._body)

        # Which window? Apps like browsers and editors often have several
        # windows open at once — everything below (read / ask / do / watch)
        # targets the one chosen here. Hidden when there's only one.
        self._window_row = QWidget()
        window_row = QHBoxLayout(self._window_row)
        window_row.setContentsMargins(0, 0, 0, 0)
        window_row.addWidget(QLabel("Window:"))
        self._window_combo = _NoWheelComboBox()
        _allow_narrow(self._window_combo)
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)
        window_row.addWidget(self._window_combo, 1)
        self._window_row.hide()
        layout.addWidget(self._window_row)

        read_group = QGroupBox("Read text on screen")
        rg = QVBoxLayout(read_group)
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
        # The screenshot that was read — so the user sees exactly what winSpark
        # captured (and can tell why some text did or didn't come through).
        self._shot_label = QLabel()
        self._shot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._shot_label.setStyleSheet("border: 1px solid #e2e8f0; border-radius: 6px; padding: 2px; background: #f8fafc;")
        self._shot_label.hide()
        rg.addWidget(self._shot_label)
        self._ocr_view = QPlainTextEdit()
        self._ocr_view.setReadOnly(True)
        self._ocr_view.setPlaceholderText("Press “Read text on screen” to capture what this app is showing.")
        rg.addWidget(self._ocr_view)
        self._ocr_status = QLabel()
        self._ocr_status.setStyleSheet("color: #64748b;")
        rg.addWidget(self._ocr_status)
        layout.addWidget(read_group)

        # Ask AI about what's on this app's screen (Comet-style assistant):
        # capture + OCR the window, then answer the question with AI.
        ask_group = QGroupBox("Ask AI about this app")
        ag = QVBoxLayout(ask_group)
        ask_hint = QLabel("Ask about what this app is showing — winSpark reads the screen and answers.")
        ask_hint.setWordWrap(True)
        ag.addWidget(ask_hint)
        ask_row = QHBoxLayout()
        self._question = QLineEdit()
        self._question.setPlaceholderText("e.g. Summarize what's on the screen, or What's the total?")
        self._question.returnPressed.connect(self.ask_ai)
        self._ask_btn = QPushButton("Ask")
        self._ask_btn.setObjectName("primary")
        self._ask_btn.clicked.connect(self.ask_ai)
        ask_row.addWidget(self._question, 1)
        ask_row.addWidget(self._ask_btn)
        ag.addLayout(ask_row)
        self._answer_view = QPlainTextEdit()
        self._answer_view.setReadOnly(True)
        self._answer_view.setPlaceholderText("The answer will appear here.")
        self._answer_view.setFixedHeight(100)
        ag.addWidget(self._answer_view)
        self._ask_status = QLabel()
        self._ask_status.setStyleSheet("color: #64748b;")
        ag.addWidget(self._ask_status)
        layout.addWidget(ask_group)

        # Do something in this app — the acting agent: instruction → AI plan
        # from the app's real controls → (approval for risky steps) → execute.
        do_group = QGroupBox("Do something in this app")
        dg = QVBoxLayout(do_group)
        self._agent_input = QLineEdit()
        self._agent_input.setPlaceholderText("Tell winSpark what to do — e.g. click Save, or: search for shoes")
        self._agent_input.returnPressed.connect(self.do_it)
        dg.addWidget(self._agent_input)
        do_row = QHBoxLayout()
        self._do_btn = QPushButton("Do it")
        self._do_btn.setObjectName("primary")
        self._do_btn.clicked.connect(self.do_it)
        do_row.addWidget(self._do_btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self.stop_agent)
        do_row.addWidget(self._stop_btn)
        do_row.addWidget(QLabel("When acting:"))
        self._agent_mode = _NoWheelComboBox()
        _allow_narrow(self._agent_mode)
        self._agent_mode.addItem("Ask before risky steps", "ask_risky")
        self._agent_mode.addItem("Just do it", "auto")
        self._agent_mode.setCurrentIndex(max(0, self._agent_mode.findData(self._controller.get_agent_mode())))
        self._agent_mode.currentIndexChanged.connect(self._on_agent_mode_changed)
        do_row.addWidget(self._agent_mode, 1)
        dg.addLayout(do_row)
        self._agent_view = QPlainTextEdit()
        self._agent_view.setReadOnly(True)
        self._agent_view.setPlaceholderText("The steps winSpark plans to take (and what happened) will appear here.")
        self._agent_view.setFixedHeight(110)
        dg.addWidget(self._agent_view)
        confirm_row = QHBoxLayout()
        self._agent_run_btn = QPushButton("Run this step")
        self._agent_run_btn.setObjectName("primary")
        self._agent_run_btn.clicked.connect(self._approve_step)
        self._agent_cancel_btn = QPushButton("Stop")
        self._agent_cancel_btn.clicked.connect(self._reject_step)
        confirm_row.addWidget(self._agent_run_btn)
        confirm_row.addWidget(self._agent_cancel_btn)
        confirm_row.addStretch(1)
        self._agent_confirm = QWidget()
        self._agent_confirm.setLayout(confirm_row)
        self._agent_confirm.hide()
        dg.addWidget(self._agent_confirm)
        self._agent_check = StatusCheck()
        dg.addWidget(self._agent_check)
        layout.addWidget(do_group)

        # Watch this app — winSpark keeps reading the screen on a timer and
        # acts the moment the watched text appears. Read-only (no clicking or
        # foregrounding), so it's safe to leave running on anything.
        watch_group = QGroupBox("Watch this app")
        wg = QVBoxLayout(watch_group)
        watch_hint = QLabel("winSpark keeps an eye on this app and tells you the moment something appears.")
        watch_hint.setWordWrap(True)
        wg.addWidget(watch_hint)
        self._watch_text = QLineEdit()
        self._watch_text.setPlaceholderText("What should winSpark wait for? e.g. Download complete, or: Out for delivery")
        wg.addWidget(self._watch_text)
        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("Then:"))
        self._watch_action = _NoWheelComboBox()
        _allow_narrow(self._watch_action)
        self._watch_action.addItem("Notify me", "notify")
        self._watch_action.addItem("Send a WhatsApp message", "whatsapp")
        self._watch_action.currentIndexChanged.connect(self._on_watch_action_changed)
        action_row.addWidget(self._watch_action, 1)
        wg.addLayout(action_row)
        self._watch_whatsapp = QWidget()
        ww = QVBoxLayout(self._watch_whatsapp)
        ww.setContentsMargins(0, 0, 0, 0)
        self._watch_chat = QLineEdit()
        self._watch_chat.setPlaceholderText("WhatsApp chat to message (exact name)")
        ww.addWidget(self._watch_chat)
        self._watch_message = QLineEdit()
        self._watch_message.setPlaceholderText("Message to send — leave blank to send a note about what appeared")
        ww.addWidget(self._watch_message)
        wg.addWidget(self._watch_whatsapp)
        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("How often to look:"))
        self._watch_interval = _NoWheelComboBox()
        for label, seconds in _WATCH_INTERVALS:
            self._watch_interval.addItem(label, seconds)
        interval_row.addWidget(self._watch_interval)
        interval_row.addStretch(1)
        wg.addLayout(interval_row)
        watch_btn_row = QHBoxLayout()
        watch_btn = QPushButton("Start watching")
        watch_btn.setObjectName("primary")
        watch_btn.clicked.connect(self.start_watching)
        watch_btn_row.addWidget(watch_btn)
        watch_btn_row.addStretch(1)
        wg.addLayout(watch_btn_row)
        self._watch_check = StatusCheck()
        wg.addWidget(self._watch_check)

        wg.addWidget(QLabel("Everything being watched (all apps):"))
        self._watchers_table = make_table(["App", "Watching for", "Then", "Status", ""], stretch_col=1)
        self._watchers_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._watchers_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._watchers_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._watchers_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._watchers_table.setMinimumHeight(96)
        self._watchers_table.setMaximumHeight(160)
        wg.addWidget(self._watchers_table)
        self._watchers_empty_label = QLabel("Nothing being watched yet.")
        self._watchers_empty_label.setStyleSheet("color: #64748b;")
        wg.addWidget(self._watchers_empty_label)
        layout.addWidget(watch_group)

        layout.addStretch(1)
        self._on_watch_action_changed()
        self.refresh_watchers()

    def set_app(self, app) -> None:
        self._app = app
        self._title.setText(f"<h2>{app.display_name}</h2>")
        windows = "1 window" if app.window_count == 1 else f"{app.window_count} windows"
        self._body.setText(
            f"winSpark can see {app.display_name} ({windows} open). This application doesn't have a "
            "dedicated integration yet, but you can still read what's on its screen and ask about it below."
        )
        self._populate_windows(app)
        self._clear_outputs()
        self.refresh_watchers()

    def _clear_outputs(self) -> None:
        """Old results describe a different target — wipe them on app/window switch."""
        self._ocr_view.clear()
        self._ocr_status.clear()
        self._copy_btn.setEnabled(False)
        self._shot_label.hide()
        self._question.clear()
        self._answer_view.clear()
        self._ask_status.clear()
        self._agent_input.clear()
        self._agent_view.clear()
        self._agent_check.clear_status()
        self._agent_confirm.hide()
        self._watch_check.clear_status()

    def _populate_windows(self, app, keep_selection: bool = False) -> None:
        previous = self._window_combo.currentData() if keep_selection else None
        self._window_combo.blockSignals(True)
        self._window_combo.clear()
        for handle, title in app.windows:
            label = (title or app.display_name).strip() or app.display_name
            self._window_combo.addItem(label[:70], handle)
        if previous is not None:
            index = self._window_combo.findData(previous)
            if index >= 0:
                self._window_combo.setCurrentIndex(index)
        self._window_combo.blockSignals(False)
        self._window_row.setVisible(app.window_count > 1)

    def update_app_windows(self, app) -> None:
        """Called on the periodic refresh: if the app's windows changed (one
        opened or closed, titles moved on), refresh the picker without
        disturbing the user's current choice."""
        if self._app is None or app is None:
            return
        current = tuple(self._app.windows)
        fresh = tuple(app.windows)
        if current == fresh:
            return
        self._app = app
        self._populate_windows(app, keep_selection=True)

    def _on_window_changed(self, *_args) -> None:
        self._clear_outputs()

    def _primary_handle(self) -> Optional[int]:
        if self._app is None or not self._app.window_handles:
            return None
        selected = self._window_combo.currentData()
        if selected in self._app.window_handles:
            return selected
        return self._app.window_handles[0]

    def _selected_window_title(self) -> str:
        """Title of the chosen window — used as the watcher's window hint so a
        watcher on "this window" keeps targeting it among its siblings."""
        if self._app is None or self._app.window_count < 2:
            return ""
        return (self._window_combo.currentText() or "").strip()

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
            image = None
            try:
                ok, result = self._controller.read_screen_text(handle)
                capture = getattr(self._controller, "capture_screen_image", None)
                if capture is not None:
                    image = capture(handle)
            except Exception as ex:  # noqa: BLE001
                ok, result = False, str(ex)
            self._ocr_done.emit(bool(ok), result or "", image)

        self._spawn(worker)

    def _on_ocr_done(self, ok: bool, result: str, image_bytes) -> None:
        self._ocr_busy = False
        self._read_btn.setEnabled(True)
        self._show_screenshot(image_bytes)
        if ok:
            self._ocr_view.setPlainText(result)
            self._copy_btn.setEnabled(True)
            self._ocr_status.setText("This is what winSpark captured — press “Copy text” to use the text elsewhere.")
        else:
            self._ocr_view.clear()
            self._copy_btn.setEnabled(False)
            self._ocr_status.setText(result)

    def _show_screenshot(self, image_bytes) -> None:
        if not image_bytes:
            self._shot_label.hide()
            return
        from PySide6.QtGui import QPixmap

        pixmap = QPixmap()
        if not pixmap.loadFromData(image_bytes):
            self._shot_label.hide()
            return

        # The preview looked soft because it was scaled to LOGICAL pixels and
        # tagged device-pixel-ratio 1.0; on a scaled display (e.g. 150%) Qt then
        # stretched that logical-size image across more physical pixels at paint
        # time — upscaling with no real detail. Instead: fit to the same on-screen
        # box, but render at PHYSICAL resolution (logical × the screen's ratio)
        # in one smooth resample, and tag the pixmap with that ratio so Qt paints
        # it 1:1. Never scale past the capture's own resolution (that would just
        # blur), and one KeepAspectRatio call avoids the double-resample softness.
        from PySide6.QtCore import QSize

        ratio = self._shot_label.devicePixelRatioF() or 1.0
        max_width = max(240, min(self._ocr_view.width() or 640, 720))
        max_height = 300
        phys = QSize(
            min(round(max_width * ratio), pixmap.width()),
            min(round(max_height * ratio), pixmap.height()),
        )
        pixmap = pixmap.scaled(
            phys, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        pixmap.setDevicePixelRatio(ratio)
        self._shot_label.setPixmap(pixmap)
        self._shot_label.show()

    def copy_text(self) -> None:
        text = self._ocr_view.toPlainText()
        if not text:
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        self._ocr_status.setText("Copied to the clipboard.")

    def ask_ai(self) -> None:
        handle = self._primary_handle()
        if handle is None:
            self._ask_status.setText("No window to look at.")
            return
        question = self._question.text().strip()
        if not question or self._ask_busy:
            return
        self._ask_busy = True
        self._ask_btn.setEnabled(False)
        self._ask_status.setText("Reading the screen and thinking…")

        def worker():
            try:
                ok, answer = self._controller.ask_about_screen(handle, question)
            except Exception as ex:  # noqa: BLE001
                ok, answer = False, str(ex)
            self._ask_done.emit(bool(ok), answer or "")

        self._spawn(worker)

    def _on_ask_done(self, ok: bool, answer: str) -> None:
        self._ask_busy = False
        self._ask_btn.setEnabled(True)
        if ok:
            self._answer_view.setPlainText(answer)
            self._ask_status.clear()
        else:
            self._answer_view.clear()
            self._ask_status.setText(answer)

    # --- the "Do it" agent -----------------------------------------------

    def _on_agent_mode_changed(self, *_args) -> None:
        self._controller.set_agent_mode(self._agent_mode.currentData())

    def do_it(self) -> None:
        """Run the closed-loop agent: act one step, LOOK at the app again, and
        decide the next step from what actually happened — never from an
        assumption about what a previous step should have done. The loop stops
        when the AI says the goal is done, a step fails, the user declines a
        risky step, the app stops responding to a repeated step, or the round
        budget runs out."""
        handle = self._primary_handle()
        if handle is None:
            self._agent_check.set_bad("Pick an app on the left first")
            return
        instruction = self._agent_input.text().strip()
        if not instruction:
            self._agent_check.set_bad("Tell winSpark what to do first")
            return
        if self._agent_busy:
            return
        self._agent_busy = True
        self._agent_stop.clear()
        self._do_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._agent_confirm.hide()
        self._agent_view.clear()
        self._agent_check.set_busy("Working on it, step by step…")
        app_name = self._app.display_name if self._app else ""

        def worker():
            from winspark.automation.screen_agent import describe_step

            history: list[str] = []
            last_description = None
            last_digest = None
            final_ok, final_message = False, ""
            stopped_by_user = "Stopped by you — nothing more was done."
            for _round in range(1, 9):
                if self._agent_stop.is_set():
                    final_message = stopped_by_user
                    break
                try:
                    ok, decision = self._controller.agent_next_step(handle, app_name, instruction, list(history))
                except Exception as ex:  # noqa: BLE001
                    ok, decision = False, str(ex)
                if not ok:
                    final_message = str(decision)
                    break
                if decision.done:
                    final_ok, final_message = True, decision.summary
                    break

                step = decision.step
                description = describe_step(step)
                if description == last_description and decision.screen_digest and decision.screen_digest == last_digest:
                    final_message = "Stopped — the app didn't respond to that step, and repeating it wouldn't help."
                    break
                last_description, last_digest = description, decision.screen_digest

                self._agent_progress.emit("→ " + description + ("  ⚠" if step.risky else ""))
                if step.risky and self._controller.get_agent_mode() == "ask_risky":
                    if not self._request_approval(description):
                        final_message = "Stopped before the risky step — nothing more was done."
                        break
                if self._agent_stop.is_set():
                    final_message = stopped_by_user
                    break

                try:
                    step_ok, message = self._controller.agent_execute_step(handle, step)
                except Exception as ex:  # noqa: BLE001
                    step_ok, message = False, str(ex)
                self._agent_progress.emit(("   ✓ " if step_ok else "   ✗ ") + message)
                history.append(f"{description} -> {'ok' if step_ok else 'FAILED: ' + message}")
                if not step_ok:
                    final_message = message
                    break
            else:
                final_message = "Stopped after 8 steps — try breaking the request into smaller pieces."

            try:
                self._controller.record_agent_result(final_message or "Done.")
            except Exception:  # noqa: BLE001
                pass
            self._agent_finished.emit(final_ok, final_message)

        self._spawn(worker)

    def stop_agent(self) -> None:
        """Interrupt the running loop. The current step (if one is mid-flight)
        finishes; nothing further happens. Also unblocks a pending risky-step
        approval as a decline, so a Stop during the prompt stops too."""
        self._agent_stop.set()
        self._approval_decision = False
        self._approval_event.set()
        self._agent_confirm.hide()
        self._agent_check.set_busy("Stopping…")

    def _request_approval_via_ui(self, description: str) -> bool:
        """Called from the loop worker: surface the risky step and block until
        the user decides (Run this step / Stop)."""
        self._approval_event.clear()
        self._approval_decision = False
        self._agent_ask.emit(description)
        self._approval_event.wait(timeout=300)  # unanswered = declined
        return self._approval_decision

    def _on_agent_ask(self, description: str) -> None:
        self._agent_confirm.show()
        self._agent_check.set_busy(f"Next step is risky (⚠): {description} — run it?")

    def _approve_step(self) -> None:
        self._agent_confirm.hide()
        self._agent_check.set_busy("Working on it, step by step…")
        self._approval_decision = True
        self._approval_event.set()

    def _reject_step(self) -> None:
        self._agent_confirm.hide()
        self._approval_decision = False
        self._approval_event.set()

    def _on_agent_progress(self, line: str) -> None:
        self._agent_view.appendPlainText(line)

    def _on_agent_finished(self, ok: bool, message: str) -> None:
        self._agent_busy = False
        self._do_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._agent_confirm.hide()
        if ok:
            self._agent_check.set_ok(message or "Done")
        else:
            self._agent_check.set_bad(message or "Stopped.")

    # --- screen watchers (watch any app, act when text appears) ---------

    def _on_watch_action_changed(self, *_args) -> None:
        self._watch_whatsapp.setVisible(self._watch_action.currentData() == "whatsapp")

    def start_watching(self) -> None:
        if self._app is None:
            self._watch_check.set_bad("Pick an app on the left first")
            return
        watch_text = self._watch_text.text().strip()
        if not watch_text:
            self._watch_check.set_bad("Type what winSpark should wait for")
            return
        action_kind = self._watch_action.currentData()
        chat = self._watch_chat.text().strip()
        if action_kind == "whatsapp" and not chat:
            self._watch_check.set_bad("Type which WhatsApp chat to message")
            return

        self._controller.add_watcher(
            process_name=self._app.process_name,
            title_hint=self._selected_window_title(),
            display_name=self._app.display_name,
            watch_text=watch_text,
            action_kind=action_kind,
            whatsapp_chat=chat,
            whatsapp_message=self._watch_message.text().strip(),
            interval=self._watch_interval.currentData(),
        )
        self._watch_check.set_ok(f"Watching {self._app.display_name} — you'll hear from winSpark when it appears")
        self._watch_text.clear()
        self.refresh_watchers()

    def refresh_watchers(self) -> None:
        watchers = self._controller.get_watchers()
        table = self._watchers_table
        table.setRowCount(len(watchers))
        self._watchers_empty_label.setVisible(not watchers)
        table.setVisible(bool(watchers))

        for row, watcher in enumerate(watchers):
            table.setItem(row, 0, QTableWidgetItem(watcher.app_display_name or watcher.process_name))
            table.setItem(row, 1, QTableWidgetItem(watcher.watch_text))
            then = "Notify me" if watcher.action_kind == "notify" else f"Message {watcher.whatsapp_chat}"
            table.setItem(row, 2, QTableWidgetItem(then))
            table.setItem(row, 3, QTableWidgetItem(_watcher_status_text(watcher)))

            actions = QWidget()
            actions_row = QHBoxLayout(actions)
            actions_row.setContentsMargins(2, 0, 2, 0)
            toggle_btn = QPushButton("Pause" if watcher.is_enabled else "Watch again")
            toggle_btn.clicked.connect(lambda _checked=False, w=watcher: self.toggle_watcher(w))
            remove_btn = QPushButton("Remove")
            remove_btn.clicked.connect(lambda _checked=False, w=watcher: self.remove_watcher(w))
            actions_row.addWidget(toggle_btn)
            actions_row.addWidget(remove_btn)
            table.setCellWidget(row, 4, actions)

    def toggle_watcher(self, watcher) -> None:
        self._controller.set_watcher_enabled(watcher.watcher_id, not watcher.is_enabled)
        self.refresh_watchers()

    def remove_watcher(self, watcher) -> None:
        confirm = QMessageBox.question(
            self,
            "Remove watcher",
            f"Stop watching {watcher.app_display_name or watcher.process_name} for “{watcher.watch_text}”?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._controller.delete_watcher(watcher.watcher_id)
        self.refresh_watchers()


def _watcher_status_text(watcher) -> str:
    if watcher.is_enabled:
        if watcher.status == "app-not-open":
            return "App not open"
        if watcher.status == "error":
            return "Problem"
        return "Watching"
    if watcher.status == "matched":
        return "Found it ✓"
    if watcher.status == "error":
        return "Problem"
    return "Paused"


class AutomationsPanel(QWidget):
    """Create, edit, and run saved automations. Each automation is a named
    action you set up once and run with one click: send a WhatsApp message, or
    do something in an app in plain English (e.g. "search Chrome for…"). The
    action primitives are the same ones the rest of the app uses — the WhatsApp
    sender and the closed-loop agent — so this panel is just a friendly catalog
    over them. Logic is split from widgets so it can be driven in headless tests."""

    _run_done = Signal(int, bool, str)
    _run_progress = Signal(str)

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._editing_id = None          # None while creating; an id while editing
        self._busy = False
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        # So tests can drive the delete-confirmation without a real dialog.
        self._confirm_delete = self._confirm_delete_dialog
        self._run_done.connect(self._on_run_done)
        self._run_progress.connect(self._on_run_progress)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        layout.addWidget(QLabel("<h2>Automations</h2>"))
        intro = QLabel("Set up the tasks you perform often, then run them on demand or automatically.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Master switch — pause everything from running on its own. Manual
        # "Run now" always works; this only gates schedule/screen triggers.
        from PySide6.QtWidgets import QCheckBox

        self._pause_all = QCheckBox("Pause all automatic runs")
        self._pause_all.setToolTip("Stops schedules and screen triggers from firing. You can still Run now.")
        self._pause_all.toggled.connect(self._on_pause_toggled)
        layout.addWidget(self._pause_all)
        self._pause_banner = QLabel("⏸  Automatic runs are paused — nothing will run on its own.")
        self._pause_banner.setStyleSheet("color: #b45309; background: #fef3c7; border-radius: 6px; padding: 6px 10px;")
        self._pause_banner.setWordWrap(True)
        self._pause_banner.hide()
        layout.addWidget(self._pause_banner)

        new_row = QHBoxLayout()
        self._new_btn = QPushButton("＋  New automation")
        self._new_btn.setObjectName("primary")
        self._new_btn.clicked.connect(self.new_automation)
        new_row.addWidget(self._new_btn)
        new_row.addStretch(1)
        layout.addLayout(new_row)

        self._run_check = StatusCheck()
        layout.addWidget(self._run_check)
        # Live step-by-step log while a run is happening (hidden when idle).
        self._run_log = QPlainTextEdit()
        self._run_log.setReadOnly(True)
        self._run_log.setFixedHeight(90)
        self._run_log.hide()
        layout.addWidget(self._run_log)

        # The saved automations, rebuilt on every reload().
        self._list_box = QWidget()
        self._rows = QVBoxLayout(self._list_box)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(6)
        layout.addWidget(self._list_box)
        self._empty_label = QLabel("No automations yet — create one above.")
        self._empty_label.setStyleSheet("color: #64748b;")
        layout.addWidget(self._empty_label)

        # --- the create/edit form (hidden until New/Edit) ------------------
        self._editor = QGroupBox("New automation")
        ed = QVBoxLayout(self._editor)
        ed.addWidget(QLabel("Name"))
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Morning message to Family")
        ed.addWidget(self._name)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("What should it do?"))
        self._type = _NoWheelComboBox()
        _allow_narrow(self._type)
        self._type.addItem("Send a WhatsApp message", AUTOMATION_WHATSAPP)
        self._type.addItem("Do something in an app", AUTOMATION_APP_ACTION)
        self._type.currentIndexChanged.connect(self._on_type_changed)
        type_row.addWidget(self._type, 1)
        ed.addLayout(type_row)

        # WhatsApp-message fields
        self._wa_panel = QWidget()
        wa = QVBoxLayout(self._wa_panel)
        wa.setContentsMargins(0, 0, 0, 0)
        wa.addWidget(QLabel("Chat name"))
        self._wa_chat = QLineEdit()
        self._wa_chat.setPlaceholderText("The exact chat name as shown in WhatsApp")
        wa.addWidget(self._wa_chat)
        wa.addWidget(QLabel("Message"))
        self._wa_message = QPlainTextEdit()
        self._wa_message.setPlaceholderText("The message to send")
        self._wa_message.setFixedHeight(60)
        wa.addWidget(self._wa_message)
        ed.addWidget(self._wa_panel)

        # App-action fields
        self._app_panel = QWidget()
        ap = QVBoxLayout(self._app_panel)
        ap.setContentsMargins(0, 0, 0, 0)
        ap.addWidget(QLabel("Which app?"))
        self._app_combo = _NoWheelComboBox()
        _allow_narrow(self._app_combo)
        ap.addWidget(self._app_combo)
        self._app_hint = QLabel("Open the app first so it appears here. It'll need to be open when the automation runs.")
        self._app_hint.setWordWrap(True)
        self._app_hint.setStyleSheet("color: #64748b;")
        ap.addWidget(self._app_hint)
        ap.addWidget(QLabel("What should winSpark do?"))
        self._app_instruction = QPlainTextEdit()
        self._app_instruction.setPlaceholderText("In plain English, e.g. search Google for cheap flights to Goa")
        self._app_instruction.setFixedHeight(60)
        ap.addWidget(self._app_instruction)
        ed.addWidget(self._app_panel)

        # --- when should it run? (the trigger) -----------------------------
        from PySide6.QtCore import QTime
        from PySide6.QtWidgets import QTimeEdit

        when_row = QHBoxLayout()
        when_row.addWidget(QLabel("When should it run?"))
        self._trigger = _NoWheelComboBox()
        _allow_narrow(self._trigger)
        self._trigger.addItem("When you run it", TRIGGER_MANUAL)
        self._trigger.addItem("On a schedule", TRIGGER_SCHEDULE)
        self._trigger.addItem("When an app shows text", TRIGGER_SCREEN)
        self._trigger.currentIndexChanged.connect(self._on_trigger_changed)
        when_row.addWidget(self._trigger, 1)
        ed.addLayout(when_row)

        # schedule sub-panel
        self._sched_panel = QWidget()
        sc = QVBoxLayout(self._sched_panel)
        sc.setContentsMargins(0, 0, 0, 0)
        mode_row = QHBoxLayout()
        self._sched_mode = _NoWheelComboBox()
        _allow_narrow(self._sched_mode)
        self._sched_mode.addItem("Every so often", "interval")
        self._sched_mode.addItem("Every day at a time", "daily")
        self._sched_mode.currentIndexChanged.connect(self._on_sched_mode_changed)
        mode_row.addWidget(self._sched_mode, 1)
        sc.addLayout(mode_row)
        self._interval_row = QWidget()
        ir = QHBoxLayout(self._interval_row)
        ir.setContentsMargins(0, 0, 0, 0)
        ir.addWidget(QLabel("Every"))
        self._interval = _NoWheelComboBox()
        for label, minutes in (("5 minutes", 5), ("15 minutes", 15), ("30 minutes", 30),
                               ("hour", 60), ("3 hours", 180), ("day", 1440)):
            self._interval.addItem(label, minutes)
        self._interval.setCurrentIndex(3)  # hourly default
        ir.addWidget(self._interval)
        ir.addStretch(1)
        sc.addWidget(self._interval_row)
        self._daily_row = QWidget()
        dr = QHBoxLayout(self._daily_row)
        dr.setContentsMargins(0, 0, 0, 0)
        dr.addWidget(QLabel("At"))
        self._daily_time = QTimeEdit()
        self._daily_time.setDisplayFormat("HH:mm")
        self._daily_time.setTime(QTime(9, 0))
        dr.addWidget(self._daily_time)
        dr.addStretch(1)
        sc.addWidget(self._daily_row)
        ed.addWidget(self._sched_panel)

        # screen-trigger sub-panel
        self._screen_panel = QWidget()
        sp = QVBoxLayout(self._screen_panel)
        sp.setContentsMargins(0, 0, 0, 0)
        sp.addWidget(QLabel("Watch this app"))
        self._watch_app = _NoWheelComboBox()
        _allow_narrow(self._watch_app)
        sp.addWidget(self._watch_app)
        match_row = QHBoxLayout()
        match_row.addWidget(QLabel("Match"))
        self._watch_mode = _NoWheelComboBox()
        _allow_narrow(self._watch_mode)
        self._watch_mode.addItem("the exact words", "literal")
        self._watch_mode.addItem("anything that means this (AI)", "meaning")
        match_row.addWidget(self._watch_mode, 1)
        sp.addLayout(match_row)
        sp.addWidget(QLabel("…and run when its screen shows"))
        self._watch_text = QLineEdit()
        self._watch_text.setPlaceholderText('e.g. the word "error", or describe it: "the build failed"')
        sp.addWidget(self._watch_text)
        self._watch_mode_hint = QLabel()
        self._watch_mode_hint.setWordWrap(True)
        self._watch_mode_hint.setStyleSheet("color: #64748b; font-size: 8pt;")
        sp.addWidget(self._watch_mode_hint)
        self._watch_mode.currentIndexChanged.connect(self._on_watch_mode_changed)
        ed.addWidget(self._screen_panel)

        buttons = QHBoxLayout()
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("primary")
        self._save_btn.clicked.connect(self.save)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.cancel_edit)
        buttons.addWidget(self._save_btn)
        buttons.addWidget(self._cancel_btn)
        buttons.addStretch(1)
        ed.addLayout(buttons)
        self._editor_check = StatusCheck()
        ed.addWidget(self._editor_check)
        self._editor.hide()
        layout.addWidget(self._editor)
        layout.addStretch(1)

        self.reload()

    # --- listing --------------------------------------------------------

    def reload(self) -> None:
        """Rebuild the saved-automations list from the controller."""
        self._load_pause_state()
        while self._rows.count():
            item = self._rows.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        automations = list(self._controller.get_automations())
        self._empty_label.setVisible(not automations)
        for automation in automations:
            self._rows.addWidget(self._build_row(automation))

    def _build_row(self, automation) -> QWidget:
        row = QFrame()
        row.setObjectName("card")
        row.setStyleSheet("QFrame#card { border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; }")
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 8, 10, 8)
        top = QHBoxLayout()
        name = QLabel(automation.name)
        name.setStyleSheet("font-weight: 600;")
        top.addWidget(name)
        top.addStretch(1)
        state = QLabel("On" if automation.enabled else "Off")
        state.setStyleSheet("color: %s;" % ("#0f9d58" if automation.enabled else "#94a3b8"))
        top.addWidget(state)
        v.addLayout(top)
        summary = QLabel(automation.summary())
        summary.setWordWrap(True)
        summary.setStyleSheet("color: #475569;")
        v.addWidget(summary)
        when = QLabel("⏱  " + automation.trigger_summary())
        when.setStyleSheet("color: #64748b; font-size: 8pt;")
        v.addWidget(when)

        actions = QHBoxLayout()
        run_btn = QPushButton("Run now")
        run_btn.clicked.connect(lambda _=False, a=automation: self.run_automation(a))
        run_btn.setEnabled(automation.enabled and not self._busy)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(lambda _=False, a=automation: self.edit_automation(a))
        toggle_btn = QPushButton("Pause" if automation.enabled else "Resume")
        toggle_btn.clicked.connect(lambda _=False, a=automation: self.toggle_enabled(a))
        duplicate_btn = QPushButton("Duplicate")
        duplicate_btn.clicked.connect(lambda _=False, a=automation: self.duplicate_automation(a))
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(lambda _=False, a=automation: self.confirm_delete(a))
        for b in (run_btn, edit_btn, toggle_btn, duplicate_btn, delete_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        v.addLayout(actions)
        return row

    # --- create / edit --------------------------------------------------

    def new_automation(self) -> None:
        from PySide6.QtCore import QTime

        self._editing_id = None
        self._editor.setTitle("New automation")
        self._name.clear()
        self._wa_chat.clear()
        self._wa_message.clear()
        self._app_instruction.clear()
        self._watch_text.clear()
        self._type.setCurrentIndex(0)
        self._trigger.setCurrentIndex(0)
        self._sched_mode.setCurrentIndex(0)
        self._interval.setCurrentIndex(3)
        self._daily_time.setTime(QTime(9, 0))
        self._watch_mode.setCurrentIndex(0)
        self._fill_app_combo(self._app_combo, keep=None)
        self._fill_app_combo(self._watch_app, keep=None)
        self._on_type_changed()
        self._on_trigger_changed()
        self._on_sched_mode_changed()
        self._on_watch_mode_changed()
        self._editor_check.clear_status()
        self._editor.show()

    def edit_automation(self, automation) -> None:
        from PySide6.QtCore import QTime

        self._editing_id = automation.id
        self._editor.setTitle("Edit automation")
        self._name.setText(automation.name)
        self._type.setCurrentIndex(max(0, self._type.findData(automation.kind)))
        if automation.kind == AUTOMATION_WHATSAPP:
            self._wa_chat.setText(automation.target_display or automation.target)
            self._wa_message.setPlainText(automation.instruction)
        else:
            self._fill_app_combo(self._app_combo, keep=(automation.target, automation.target_display))
            self._app_instruction.setPlainText(automation.instruction)
        # trigger
        self._trigger.setCurrentIndex(max(0, self._trigger.findData(automation.trigger_type)))
        self._sched_mode.setCurrentIndex(max(0, self._sched_mode.findData(automation.schedule_mode)))
        i = self._interval.findData(automation.interval_minutes)
        self._interval.setCurrentIndex(i if i >= 0 else 3)
        try:
            hh, mm = (int(p) for p in automation.daily_time.split(":", 1))
            self._daily_time.setTime(QTime(hh, mm))
        except (ValueError, AttributeError):
            self._daily_time.setTime(QTime(9, 0))
        self._fill_app_combo(self._watch_app, keep=(automation.watch_process, automation.watch_display))
        self._watch_text.setText(automation.watch_text)
        self._watch_mode.setCurrentIndex(max(0, self._watch_mode.findData(automation.watch_mode)))
        self._on_type_changed()
        self._on_trigger_changed()
        self._on_sched_mode_changed()
        self._on_watch_mode_changed()
        self._editor_check.clear_status()
        self._editor.show()

    def _fill_app_combo(self, combo, keep) -> None:
        """Fill an app dropdown from what's running. `keep` is an optional
        (process_name, display) to keep selectable even if it isn't open now
        (so editing an automation for a closed app still shows its target)."""
        combo.clear()
        seen = set()
        for app in self._controller.get_running_apps():
            combo.addItem(app.display_name, app.process_name)
            seen.add(app.process_name.lower())
        if keep and keep[0] and keep[0].lower() not in seen:
            combo.addItem(f"{keep[1] or keep[0]} (not open)", keep[0])
        if keep and keep[0]:
            i = combo.findData(keep[0])
            if i >= 0:
                combo.setCurrentIndex(i)

    def _on_type_changed(self, *_args) -> None:
        is_wa = self._type.currentData() == AUTOMATION_WHATSAPP
        self._wa_panel.setVisible(is_wa)
        self._app_panel.setVisible(not is_wa)

    def _on_trigger_changed(self, *_args) -> None:
        trigger = self._trigger.currentData()
        self._sched_panel.setVisible(trigger == TRIGGER_SCHEDULE)
        self._screen_panel.setVisible(trigger == TRIGGER_SCREEN)

    def _on_sched_mode_changed(self, *_args) -> None:
        interval = self._sched_mode.currentData() == "interval"
        self._interval_row.setVisible(interval)
        self._daily_row.setVisible(not interval)

    def _on_watch_mode_changed(self, *_args) -> None:
        if self._watch_mode.currentData() == "meaning":
            self._watch_mode_hint.setText("Uses the AI service from Settings to decide if the screen means this.")
        else:
            self._watch_mode_hint.setText("Fires when those exact words appear on screen.")

    # --- pause-all master switch ----------------------------------------

    def _load_pause_state(self) -> None:
        paused = bool(self._controller.get_automations_paused())
        self._pause_all.blockSignals(True)
        self._pause_all.setChecked(paused)
        self._pause_all.blockSignals(False)
        self._pause_banner.setVisible(paused)

    def _on_pause_toggled(self, checked: bool) -> None:
        self._controller.set_automations_paused(checked)
        self._pause_banner.setVisible(checked)

    def current_kind(self) -> str:
        return self._type.currentData()

    def current_trigger(self) -> str:
        return self._trigger.currentData()

    def save(self) -> None:
        name = self._name.text().strip()
        if not name:
            self._editor_check.set_bad("Give your automation a name.")
            return
        kind = self.current_kind()
        if kind == AUTOMATION_WHATSAPP:
            chat = self._wa_chat.text().strip()
            message = self._wa_message.toPlainText().strip()
            if not chat or not message:
                self._editor_check.set_bad("Enter the chat name and the message.")
                return
            target, target_display, instruction = chat, chat, message
        else:
            target = self._app_combo.currentData() or ""
            target_display = self._app_combo.currentText().replace(" (not open)", "")
            instruction = self._app_instruction.toPlainText().strip()
            if not target or not instruction:
                self._editor_check.set_bad("Pick an app and say what to do.")
                return

        trigger_type = self.current_trigger()
        watch_process = watch_display = watch_text = ""
        watch_mode = "literal"
        if trigger_type == TRIGGER_SCREEN:
            watch_process = self._watch_app.currentData() or ""
            watch_display = self._watch_app.currentText().replace(" (not open)", "")
            watch_text = self._watch_text.text().strip()
            watch_mode = self._watch_mode.currentData()
            if not watch_process or not watch_text:
                self._editor_check.set_bad("Pick an app to watch and the text to watch for.")
                return

        self._controller.save_automation(
            self._editing_id, name, kind, target, target_display, instruction,
            trigger_type=trigger_type,
            schedule_mode=self._sched_mode.currentData(),
            interval_minutes=self._interval.currentData(),
            daily_time=self._daily_time.time().toString("HH:mm"),
            watch_process=watch_process,
            watch_display=watch_display,
            watch_text=watch_text,
            watch_mode=watch_mode,
        )
        self._editor.hide()
        self.reload()

    def cancel_edit(self) -> None:
        self._editor.hide()

    def toggle_enabled(self, automation) -> None:
        self._controller.set_automation_enabled(automation.id, not automation.enabled)
        self.reload()

    def duplicate_automation(self, automation) -> None:
        """Save a copy as a brand-new (manual, so a copied schedule doesn't
        quietly start firing) automation the user can then tweak."""
        self._controller.save_automation(
            None, f"{automation.name} (copy)", automation.kind,
            automation.target, automation.target_display, automation.instruction,
        )
        self.reload()

    def confirm_delete(self, automation) -> None:
        if self._confirm_delete(automation):
            self.delete_automation(automation)

    def _confirm_delete_dialog(self, automation) -> bool:
        answer = QMessageBox.question(
            self, "Delete automation?",
            f"Delete “{automation.name}”? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def delete_automation(self, automation) -> None:
        self._controller.delete_automation(automation.id)
        self.reload()

    # --- running --------------------------------------------------------

    def run_automation(self, automation) -> None:
        if self._busy:
            return
        self._busy = True
        self._run_log.clear()
        self._run_log.show()
        self._run_check.set_busy(f"Running “{automation.name}”…")
        self.reload()  # disables Run buttons while busy

        automation_id = automation.id

        def worker():
            try:
                ok, message = self._controller.run_automation(
                    automation_id, progress=lambda line: self._run_progress.emit(line)
                )
            except Exception as ex:  # noqa: BLE001
                ok, message = False, str(ex)
            self._run_done.emit(automation_id, ok, message)

        self._spawn(worker)

    def _on_run_progress(self, line: str) -> None:
        self._run_log.appendPlainText(line)

    def _on_run_done(self, automation_id: int, ok: bool, message: str) -> None:
        self._busy = False
        if ok:
            self._run_check.set_ok(message or "Done.")
        else:
            self._run_check.set_bad(message or "It didn't finish.")
        self.reload()
        # Running an automation drives another app to the front (WhatsApp, or
        # the target app), which pushes winSpark behind it — so once the run is
        # done, bring winSpark back so the user lands on the Automations view
        # again instead of a window that seems to have vanished.
        window = self.window()
        if window is not None:
            window.raise_()
            window.activateWindow()


class SettingsPanel(QWidget):
    """App-wide settings — one unambiguous home for the AI service that powers
    everything (AI replies, semantic watching, asking about screens, and the
    "Do it" agent). Previously these fields sat inside the WhatsApp panel,
    which made an app-wide setting look WhatsApp-specific."""

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Settings</h2>"))

        ai_group = QGroupBox("AI service")
        ag = QVBoxLayout(ai_group)
        ai_hint = QLabel(
            "One AI service powers everything: AI replies in WhatsApp, matching by meaning, "
            "asking about screens, and the “Do it” agent."
        )
        ai_hint.setWordWrap(True)
        ai_hint.setStyleSheet("color: #64748b;")
        ag.addWidget(ai_hint)

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("Service:"))
        self._provider = _NoWheelComboBox()
        self._provider.addItem("OpenAI", "openai")
        self._provider.addItem("Groq", "groq")
        self._provider.currentIndexChanged.connect(self._on_provider_changed)
        provider_row.addWidget(self._provider, 1)
        ag.addLayout(provider_row)

        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("Your API key")
        self._key.textChanged.connect(lambda _: self._check.clear_status())
        ag.addWidget(self._key)

        self._model = QLineEdit()
        ag.addWidget(self._model)

        buttons = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self.save)
        test_btn = QPushButton("Test connection")
        test_btn.clicked.connect(self.test_connection)
        buttons.addWidget(save_btn)
        buttons.addWidget(test_btn)
        buttons.addStretch(1)
        ag.addLayout(buttons)

        self._check = StatusCheck()
        ag.addWidget(self._check)
        layout.addWidget(ai_group)
        layout.addStretch(1)

        self.reload()

    def reload(self) -> None:
        """Refresh fields from the saved settings (called when the panel opens)."""
        self._provider.blockSignals(True)
        self._provider.setCurrentIndex(max(0, self._provider.findData(self._controller.get_ai_provider())))
        self._provider.blockSignals(False)
        self._key.setText(self._controller.get_openai_api_key())
        self._model.setText(self._controller.get_openai_model())
        self._on_provider_changed()
        self._check.clear_status()

    def _on_provider_changed(self, *_args) -> None:
        # Keys/models are per provider — load the selected provider's own saved
        # values so the key box shows that provider's key (empty if none), not
        # whatever was there for the other provider.
        provider = self._provider.currentData()
        default_model = ai_provider_info(provider)["default_model"]
        self._model.setPlaceholderText(f"Model (blank = {default_model})")
        self._key.setText(self._controller.get_openai_api_key(provider))
        self._model.setText(self._controller.get_openai_model(provider))
        self._check.clear_status()

    def save(self) -> None:
        self._controller.set_openai_config(
            self._key.text().strip(), self._model.text().strip(), self._provider.currentData()
        )
        self._model.setText(self._controller.get_openai_model())
        self._check.set_ok("Saved")

    def test_connection(self) -> None:
        self._controller.set_openai_config(
            self._key.text().strip(), self._model.text().strip(), self._provider.currentData()
        )
        self._check.set_busy("Testing connection…")
        ok, detail = self._controller.test_openai_connection()
        if ok:
            self._check.set_ok("Connected — saved")
        else:
            self._check.set_bad(detail)


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
