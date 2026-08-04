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


def _next_duplicate_name(name: str, existing: set[str]) -> str:
    """The next free "<base> (N)" name for a duplicate. "Morning msg" →
    "Morning msg (1)"; if that's taken, "(2)", and so on. If `name` is already
    "<base> (K)", numbering continues from the base rather than nesting
    "(K) (1)"."""
    import re

    base = re.sub(r"\s*\(\d+\)$", "", name).strip() or name
    n = 1
    while f"{base} ({n})" in existing:
        n += 1
    return f"{base} ({n})"


def _collapsible(box, body, start_open: bool) -> None:
    """Make a card collapse to just its title. The title's checkbox is the
    toggle; the body (one widget holding all content) hides when unchecked, so
    conditional visibility INSIDE the card keeps working while it's open."""
    box.setCheckable(True)
    box.setChecked(start_open)
    body.setVisible(start_open)
    box.toggled.connect(body.setVisible)


def _looks_like_url(url: str) -> bool:
    """Enough validation to catch a mangled paste before something starts
    polling it: an http(s) scheme and a host."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _describe_binding_method(binding) -> str:
    """Plain-English summary of what a saved automation does, for the
    "Your automations" list."""
    if binding.reply_source == "trigger":
        phrase = binding.trigger_text.strip() or "…"
        return f'Watch for "{phrase}"'
    if binding.reply_source == "openai":
        if binding.ai_mode == "command":
            word = binding.trigger_text.strip() or "…"
            return f'AI answers "{AI_COMMAND_PREFIX}{word}"'
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

from winspark.constants import AI_COMMAND_PREFIX, DEFAULT_CHAT_MEMORY_MONGO_DB, ai_provider_info
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
    _binding_op_done = Signal()  # a start/stop/pause/remove finished — refresh
    _open_done = Signal(bool, str)
    _chats_ready = Signal(object)

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._chats: list = []
        self._msg_busy = False
        self._action_busy = False
        self._active_chat_name = ""   # the chat currently open in WhatsApp (what has memory)
        self._chats_busy = False
        # Overridable so tests can run "background" work inline/synchronously.
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        self._binding_busy = False
        self._messages_ready.connect(self._on_messages_ready)
        self._send_done.connect(self._on_send_done)
        self._binding_op_done.connect(self._on_binding_op_done)
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

        # Chat — a search box with suggestions. Picking a suggestion (or a
        # recent chat, shown on demand) opens the chat in WhatsApp, reads it,
        # and comes back to winSpark. No separate check/confirm step.
        step1 = QGroupBox("Chat")
        _s1_outer = QVBoxLayout(step1)
        _s1_body = QWidget()
        s1 = QVBoxLayout(_s1_body)
        s1.setContentsMargins(0, 0, 0, 0)
        _s1_outer.addWidget(_s1_body)
        _collapsible(step1, _s1_body, start_open=True)
        row1 = QHBoxLayout()
        self._chat_name = QLineEdit()
        self._chat_name.setPlaceholderText("Search chats, or type a name or number…")
        self._chat_name.textChanged.connect(self._on_chat_name_changed)
        self._chat_name.returnPressed.connect(self._verify_and_open)
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QCompleter

        self._chat_completer = QCompleter([])
        self._chat_completer.setCaseSensitivity(_Qt.CaseInsensitive)
        self._chat_completer.setFilterMode(_Qt.MatchContains)
        self._chat_completer.activated.connect(self._select_chat)
        self._chat_name.setCompleter(self._chat_completer)
        # The last URL we auto-filled into the source box — so we can refresh it
        # when the chat changes WITHOUT clobbering a URL the user typed.
        self._source_autofilled = ""
        row1.addWidget(self._chat_name, 1)
        self._ok_btn = QPushButton("OK")
        self._ok_btn.setObjectName("primary")
        self._ok_btn.setToolTip("Check the chat exists, then open it")
        self._ok_btn.clicked.connect(self._verify_and_open)
        row1.addWidget(self._ok_btn)
        self._recents_btn = QPushButton("Recents")
        self._recents_btn.setCheckable(True)
        self._recents_btn.toggled.connect(self._toggle_recents)
        row1.addWidget(self._recents_btn)
        self._refresh_chats_btn = QPushButton("↻")
        self._refresh_chats_btn.setToolTip("Refresh chats")
        self._refresh_chats_btn.setFixedWidth(34)
        self._refresh_chats_btn.clicked.connect(self.refresh_chats)
        row1.addWidget(self._refresh_chats_btn)
        s1.addLayout(row1)
        self._chat_list = QListWidget()
        self._chat_list.setMaximumHeight(140)
        self._chat_list.itemClicked.connect(self._on_chat_list_item_clicked)
        self._chat_list.hide()   # shown by the Recents button, not by default
        s1.addWidget(self._chat_list)
        self._chat_check = StatusCheck()
        s1.addWidget(self._chat_check)
        layout.addWidget(step1)

        # Replies — from a web link, or written by AI. (String matching was a
        # third option here; AI watching covers it better, so it's gone.)
        step2 = QGroupBox("Replies")
        _s2_outer = QVBoxLayout(step2)
        _s2_body = QWidget()
        s2 = QVBoxLayout(_s2_body)
        s2.setContentsMargins(0, 0, 0, 0)
        _s2_outer.addWidget(_s2_body)
        _collapsible(step2, _s2_body, start_open=True)

        self._method_combo = _NoWheelComboBox()
        _allow_narrow(self._method_combo)
        self._method_combo.addItem("From a web link", "web")
        self._method_combo.addItem("Written by AI", "openai")
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        s2.addWidget(self._method_combo)

        # Web-link sub-panel. The chat's test link is always filled in; typing
        # replaces it with your own link (checked before starting).
        self._web_panel = QWidget()
        web = QVBoxLayout(self._web_panel)
        web.setContentsMargins(0, 0, 0, 0)
        self._source = QLineEdit()
        self._source.setPlaceholderText("Web link that provides replies")
        self._source.textChanged.connect(lambda _: self._source_check.clear_status())
        web.addWidget(self._source)
        web_hint = QLabel("Text sent to this link goes to the chat. Or paste your own link.")
        web_hint.setWordWrap(True)
        web_hint.setStyleSheet("color: #64748b; font-size: 8pt;")
        web.addWidget(web_hint)
        from PySide6.QtWidgets import QCheckBox

        self._webhook_testing = QCheckBox("Let this link send to the chat")
        self._webhook_testing.toggled.connect(self._on_webhook_testing_toggled)
        web.addWidget(self._webhook_testing)
        self._webhook_status = QLabel()
        self._webhook_status.setStyleSheet("color: #64748b; font-size: 8pt;")
        web.addWidget(self._webhook_status)
        # Try it: type something, it goes through the link into the chat.
        test_row = QHBoxLayout()
        self._test_message = QLineEdit()
        self._test_message.setPlaceholderText("Try it — send a test message")
        self._test_message.returnPressed.connect(self._send_test_message)
        test_row.addWidget(self._test_message, 1)
        test_send_btn = QPushButton("Send test")
        test_send_btn.clicked.connect(self._send_test_message)
        test_row.addWidget(test_send_btn)
        web.addLayout(test_row)
        web_test = QPushButton("Test link")
        web_test.clicked.connect(self.test_source)
        self._copy_link_btn = QPushButton("Copy link")
        self._copy_link_btn.clicked.connect(self._copy_source_link)
        web_row = QHBoxLayout()
        web_row.addWidget(web_test)
        web_row.addWidget(self._copy_link_btn)
        web_row.addStretch(1)
        web.addLayout(web_row)
        s2.addWidget(self._web_panel)

        # AI sub-panel — mode + prompt only. The AI service itself (provider,
        # key, model) is app-wide and lives in Settings.
        self._ai_panel = QWidget()
        ai = QVBoxLayout(self._ai_panel)
        ai.setContentsMargins(0, 0, 0, 0)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("When:"))
        self._ai_mode = _NoWheelComboBox()
        _allow_narrow(self._ai_mode)
        self._ai_mode.addItem("Reply to each new message", "reply")
        self._ai_mode.addItem("Post a new message every check", "generate")
        self._ai_mode.addItem(f"Only when someone types {AI_COMMAND_PREFIX}name", "command")
        self._ai_mode.currentIndexChanged.connect(self._on_ai_mode_changed)
        mode_row.addWidget(self._ai_mode, 1)
        ai.addLayout(mode_row)

        # Command-word row — only shown for the "command" mode above.
        self._ai_command_row = QWidget()
        cmd_row = QHBoxLayout(self._ai_command_row)
        cmd_row.setContentsMargins(0, 0, 0, 0)
        cmd_row.addWidget(QLabel("Call it:"))
        self._ai_command_prefix = QLabel(AI_COMMAND_PREFIX)
        self._ai_command_prefix.setStyleSheet("font-weight: 700;")
        cmd_row.addWidget(self._ai_command_prefix)
        self._ai_command = QLineEdit()
        self._ai_command.setPlaceholderText("winspark")
        cmd_row.addWidget(self._ai_command, 1)
        ai.addWidget(self._ai_command_row)
        self._ai_prompt = QPlainTextEdit()
        self._ai_prompt.setPlaceholderText("Optional: how should it write? e.g. Warm and brief.")
        self._ai_prompt.setFixedHeight(60)
        ai.addWidget(self._ai_prompt)
        self._ai_mode_hint = QLabel()
        self._ai_mode_hint.setWordWrap(True)
        self._ai_mode_hint.setStyleSheet("color: #64748b;")
        ai.addWidget(self._ai_mode_hint)
        s2.addWidget(self._ai_panel)

        self._source_check = StatusCheck()
        s2.addWidget(self._source_check)

        # …and how often, plus the on/off switch — one row, not two steps.
        run_row = QHBoxLayout()
        run_row.addWidget(QLabel("Check:"))
        self._interval_combo = _NoWheelComboBox()
        for label, seconds in _CHECK_INTERVALS:
            self._interval_combo.addItem(label, seconds)
        run_row.addWidget(self._interval_combo)
        self._start_button = QPushButton("Start")
        self._start_button.setObjectName("primary")
        self._start_button.clicked.connect(self.toggle_automation)
        run_row.addWidget(self._start_button)
        self._run_status = QLabel()
        run_row.addWidget(self._run_status, 1)
        s2.addLayout(run_row)
        layout.addWidget(step2)
        self._on_method_changed()
        self._on_ai_mode_changed()

        # Your automations — every chat that has automation configured, not just
        # the one currently selected above, so you can see what's running and
        # pause/stop ("disband") anything you don't want running anymore.
        auto_group = QGroupBox("Your automations")
        _ag_outer = QVBoxLayout(auto_group)
        _ag_body = QWidget()
        ag2 = QVBoxLayout(_ag_body)
        ag2.setContentsMargins(0, 0, 0, 0)
        _ag_outer.addWidget(_ag_body)
        _collapsible(auto_group, _ag_body, start_open=False)
        self._automations_table = make_table(["Chat", "What it does", "Status", ""], stretch_col=1)
        self._automations_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._automations_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._automations_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._automations_table.setMinimumHeight(96)
        self._automations_table.setMaximumHeight(160)
        ag2.addWidget(self._automations_table)
        self._automations_empty_label = QLabel("None yet.")
        self._automations_empty_label.setStyleSheet("color: #64748b;")
        ag2.addWidget(self._automations_empty_label)
        layout.addWidget(auto_group)

        # What winSpark remembers about the selected chat — the rolling memory
        # AI replies draw on. View-only and PERSISTENT: memory is never cleared
        # from here, so context carries across restarts and re-added automations.
        mem_group = QGroupBox("Memory")
        _mg_outer = QVBoxLayout(mem_group)
        _mg_body = QWidget()
        mg = QVBoxLayout(_mg_body)
        mg.setContentsMargins(0, 0, 0, 0)
        _mg_outer.addWidget(_mg_body)
        _collapsible(mem_group, _mg_body, start_open=False)
        self._memory_hint = QLabel("What winSpark remembers for this chat — AI replies use it.")
        self._memory_hint.setWordWrap(True)
        self._memory_hint.setStyleSheet("color: #64748b;")
        mg.addWidget(self._memory_hint)
        self._memory_view = QPlainTextEdit()
        self._memory_view.setReadOnly(True)
        self._memory_view.setPlaceholderText("Nothing yet.")
        self._memory_view.setFixedHeight(110)
        mg.addWidget(self._memory_view)
        mem_row = QHBoxLayout()
        self._memory_backend_label = QLabel()
        self._memory_backend_label.setStyleSheet("color: #64748b; font-size: 8pt;")
        mem_row.addWidget(self._memory_backend_label, 1)
        self._memory_refresh_btn = QPushButton("Refresh")
        self._memory_refresh_btn.clicked.connect(self._refresh_memory_view)
        mem_row.addWidget(self._memory_refresh_btn)
        mg.addLayout(mem_row)
        layout.addWidget(mem_group)

        # Messages — a live view of the open chat + a box to send one yourself.
        convo = QGroupBox("Messages")
        _cv_outer = QVBoxLayout(convo)
        _cv_body = QWidget()
        cv = QVBoxLayout(_cv_body)
        cv.setContentsMargins(0, 0, 0, 0)
        _cv_outer.addWidget(_cv_body)
        _collapsible(convo, _cv_body, start_open=False)
        self._messages_view = QPlainTextEdit()
        self._messages_view.setReadOnly(True)
        self._messages_view.setPlaceholderText("The open chat's messages appear here.")
        self._messages_view.setFixedHeight(120)
        cv.addWidget(self._messages_view)
        self._messages_status = QLabel()
        self._messages_status.setStyleSheet("color: #64748b;")
        cv.addWidget(self._messages_status)
        self._compose = QLineEdit()
        self._compose.setPlaceholderText("Send a message…")
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

        # The cards, by name, so state (collapsed/expanded) is inspectable.
        self._chat_card, self._replies_card = step1, step2
        self._automations_card, self._memory_card, self._messages_card = auto_group, mem_group, convo

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
        # Feed the search box's suggestions from the same list.
        if hasattr(self, "_chat_completer"):
            from PySide6.QtCore import QStringListModel

            self._chat_completer.setModel(QStringListModel([c.chat_name for c in self._chats]))
        self.refresh()

    def _toggle_recents(self, checked: bool) -> None:
        """Show or hide the recent-chats list — hidden by default so the panel
        opens to just a search box, not a wall of chats."""
        self._chat_list.setVisible(checked)
        if checked and self._chat_list.count() == 0:
            self.refresh_chats()

    def _select_chat(self, name: str) -> None:
        """A chat was picked (suggestion or recents): put it in the field and
        open it — WhatsApp shows the chat, winSpark reads it, focus comes back
        here. One action, no separate check/open steps."""
        self._chat_name.setText(name)
        self._chat_check.clear_status()
        self._open_selected_chat()

    def _open_selected_chat(self) -> None:
        if self.current_chat():
            self.open_chat()

    def _on_chat_list_item_clicked(self, item) -> None:
        self._select_chat(item.text())

    def current_chat(self) -> str:
        return self._chat_name.text().strip()

    def _default_source_url(self) -> str:
        """The built-in inbox URL for the chosen chat — POST text here and it's
        forwarded to that chat."""
        from winspark.connectors.fetch_webhook_models import FetchWebhookDefaults

        chat = self.current_chat()
        return FetchWebhookDefaults.mock_url_for_group(chat) if chat else ""

    def _on_chat_name_changed(self, *_args) -> None:
        self._chat_check.clear_status()
        if not hasattr(self, "_source"):
            return  # step 2's field is built after step 1's — nothing to fill yet
        # Keep the built-in inbox link pointed at the current chat — but only
        # while the source box still holds a link we filled in (empty, or the
        # default for a previous chat). If the user typed their own address, we
        # leave it alone.
        current = self._source.text().strip()
        if current == "" or current == self._source_autofilled:
            new_url = self._default_source_url()
            self._source_autofilled = new_url
            self._source.blockSignals(True)
            self._source.setText(new_url)
            self._source.blockSignals(False)

    def _copy_source_link(self) -> None:
        link = self._source.text().strip() or self._default_source_url()
        if not link:
            self._source_check.set_bad("Choose a chat first")
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(link)
        self._source_check.set_ok("Link copied — POST your text to it to message this chat")

    def selected_interval(self) -> int:
        return self._interval_combo.currentData()

    def check_chat(self) -> bool:
        """Verify the typed chat exists, showing ✓/✗. Returns whether it did."""
        chat = self.current_chat()
        if not chat:
            self._chat_check.set_bad("Pick a chat first")
            return False
        self._chat_check.set_busy("Looking…")
        # Resolve to the canonical contact/chat name. If the user typed a phone
        # number bound to a saved contact, switch the field to the CONTACT name —
        # so sends/opens use the recents row and memory unifies under the name.
        canonical = ""
        if hasattr(self._controller, "resolve_chat_name"):
            canonical = (self._controller.resolve_chat_name(chat) or "").strip()
        if canonical:
            if canonical != chat:
                self._chat_name.setText(canonical)
                self._chat_check.set_ok(f"Found — using “{canonical}”")
            else:
                self._chat_check.set_ok("Found")
            return True
        if self._controller.can_find_chat(chat):
            self._chat_check.set_ok("Found")
            return True
        self._chat_check.set_bad("Couldn't find this chat")
        return False

    def _verify_and_open(self) -> None:
        """OK (or Enter) on the search box: verify the chat, show the result,
        and open it only when it was actually found."""
        if self.check_chat():
            self._open_selected_chat()

    def current_reply_source(self) -> str:
        return self._method_combo.currentData()

    def _on_method_changed(self, *_args) -> None:
        source = self.current_reply_source()
        self._web_panel.setVisible(source == "web")
        self._ai_panel.setVisible(source == "openai")
        self._source_check.clear_status()
        # The Start/Stop button follows the selected type — switching to a type
        # that isn't running yet offers "Start" even if another type is already
        # on for this chat.
        if hasattr(self, "_start_button"):
            self._start_button.setText("Stop" if self.is_running() else "Start")

    def current_ai_mode(self) -> str:
        return self._ai_mode.currentData()

    def _on_ai_mode_changed(self, *_args) -> None:
        mode = self.current_ai_mode()
        self._ai_command_row.setVisible(mode == "command")
        if mode == "reply":
            self._ai_mode_hint.setText("Answers each new message once. Never replies to you.")
        elif mode == "command":
            word = self._ai_command.text().strip().lstrip("!@/#").strip() or "winspark"
            self._ai_mode_hint.setText(
                f"Silent until someone writes {AI_COMMAND_PREFIX}{word} — then it answers them."
            )
        else:
            self._ai_mode_hint.setText(
                "Posts a new message every check — use a long interval."
            )

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
        """Is the SELECTED type of automation on for this chat? A chat can run
        one automation per type (web link, AI, trigger) side by side — the
        Start/Stop button follows the type picked in step 2."""
        chat = self.current_chat()
        return bool(chat) and self._controller.is_chat_automation_running(chat, self.current_reply_source())

    def toggle_automation(self) -> None:
        chat = self.current_chat()
        if not chat:
            self._chat_check.set_bad("Choose a chat first")
            return
        source = self.current_reply_source()
        # Gather everything from the widgets NOW (UI thread), then do the
        # actual start/stop on a worker: these calls go through the engine and
        # can stall for seconds when WhatsApp is busy — running them here froze
        # the whole window (seen live as "Not Responding" during a delete).
        stopping = self.is_running()
        interval = self.selected_interval()
        url = self._source.text().strip() or self._default_source_url()
        ai_mode = self.current_ai_mode()
        ai_prompt = self._ai_prompt.toPlainText().strip()
        # Tolerate the prefix being typed into the field ("!winspark"): it's the
        # obvious thing to do, and storing it would make the matcher look for
        # "!!winspark".
        command_word = self._ai_command.text().strip().lstrip("!@/#").strip()

        if ai_mode == "command" and not stopping and not command_word:
            self._chat_check.set_bad(
                f"Type the word people will use to call it, e.g. {AI_COMMAND_PREFIX}winspark")
            return
        # A typed link must at least look like one before anything starts
        # polling it — the test link is always valid, so this only fires on
        # the user's own address.
        if source == "web" and not stopping and not _looks_like_url(url):
            self._source_check.set_bad("That link doesn't look right — it should start with https://")
            return

        def op():
            if stopping:
                self._controller.stop_chat_automation(chat, source)
            elif source == "openai":
                self._controller.start_chat_automation(
                    chat, "", interval, reply_source="openai", ai_mode=ai_mode, ai_prompt=ai_prompt,
                    trigger_text=command_word if ai_mode == "command" else "",
                )
            else:
                self._controller.start_chat_automation(chat, url, interval)

        self._run_binding_op(op, "Stopping…" if stopping else "Starting…")

    def _send_test_message(self) -> None:
        """Web link only: push a line of text through the link into the chat,
        so trying it out is one field and one button."""
        chat = self.current_chat()
        text = self._test_message.text().strip()
        if not chat:
            self._source_check.set_bad("Pick a chat first")
            return
        if not text:
            return
        sender = getattr(self._controller, "send_test_to_source", None)
        if sender is None:
            return
        sender(chat, text)
        self._test_message.clear()
        self._source_check.set_ok("Sent — it will arrive on the next check")

    _SOURCE_LABELS = {"web": "web link", "openai": "AI reply", "trigger": "message trigger"}

    def refresh(self) -> None:
        running = self.is_running()   # the SELECTED type, on this chat
        self._start_button.setText("Stop" if running else "Start")
        chat = self.current_chat()
        if not chat:
            self._run_status.setText("")
        else:
            # A chat can run several automations (one per type) — show them all.
            active = [
                self._SOURCE_LABELS.get(b.reply_source, b.reply_source)
                for b in self._chat_bindings(chat) if b.is_enabled
            ] if self._controller.is_relay_enabled() else []
            if active:
                self._run_status.setText(f"On — {' + '.join(active)} in “{chat}”.")
            else:
                self._run_status.setText("Off.")
        self._refresh_webhook_status()
        self.refresh_automations()
        self._refresh_memory_view()

    _ROLE_LABELS = {"me": "winSpark", "them": "them"}

    def _refresh_memory_view(self) -> None:
        """Show what winSpark remembers for the selected chat + which backend
        stores it. Degrades quietly if the controller predates chat memory."""
        if not hasattr(self._controller, "get_chat_memory"):
            return
        backend = ""
        if hasattr(self._controller, "chat_memory_backend"):
            backend = self._controller.chat_memory_backend()
        # Prefer the selected chat's memory; if it has none, fall back to the
        # chat actually open in WhatsApp — memory is stored under WhatsApp's
        # conversation title (often a phone number), which may differ from the
        # name you picked. This is why the viewer looked "empty" before.
        selected = self.current_chat()
        chat = selected
        memory = self._controller.get_chat_memory(selected) if selected else []
        active = getattr(self, "_active_chat_name", "")
        if not memory and active and active != selected:
            memory = self._controller.get_chat_memory(active)
            if memory:
                chat = active
        if not chat:
            self._memory_view.clear()
            self._memory_backend_label.setText(f"Stored in {backend}." if backend else "")
            return
        lines = [
            f"{self._ROLE_LABELS.get(role, role)}"
            + (f" ({sender})" if sender and role == "them" else "")
            + f": {text}"
            for role, sender, text in memory
        ]
        self._memory_view.setPlainText("\n".join(lines))
        count = len(memory)
        where = f" · stored in {backend}" if backend else ""
        self._memory_backend_label.setText(
            (f"{count} message{'s' if count != 1 else ''} remembered for “{chat}”" if count
             else f"Nothing remembered for “{chat}” yet") + where
        )

    def _chat_bindings(self, chat: str) -> list:
        getter = getattr(self._controller, "get_chat_bindings", None)
        if getter is not None:
            return getter(chat)
        target = chat.strip().lower()
        return [b for b in self._controller.get_bindings() if b.group_name.strip().lower() == target]

    def _on_webhook_testing_toggled(self, checked: bool) -> None:
        if hasattr(self._controller, "set_webhook_testing_enabled"):
            self._controller.set_webhook_testing_enabled(checked)

    def _refresh_webhook_status(self) -> None:
        """Reflect the webhook-testing setting + how many posted messages are
        waiting to be sent (drains as they go out, one at a time)."""
        if not hasattr(self._controller, "get_webhook_testing_enabled"):
            return
        enabled = bool(self._controller.get_webhook_testing_enabled())
        if self._webhook_testing.isChecked() != enabled:
            self._webhook_testing.blockSignals(True)
            self._webhook_testing.setChecked(enabled)
            self._webhook_testing.blockSignals(False)
        pending = 0
        if hasattr(self._controller, "webhook_pending_count"):
            pending = self._controller.webhook_pending_count()
        if pending:
            self._webhook_status.setText(f"⏳ {pending} message(s) posted to the link, sending…")
        else:
            self._webhook_status.setText("")

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

    def _run_binding_op(self, op, busy_text: str) -> None:
        """Run a start/stop/pause/remove on a worker thread so the window stays
        responsive, then refresh. One at a time — repeat clicks are ignored
        while one is in flight."""
        if self._binding_busy:
            return
        self._binding_busy = True
        self._run_status.setText(busy_text)

        def worker():
            try:
                op()
            except Exception:  # noqa: BLE001 - refresh shows the real state
                logger_ = __import__("logging").getLogger(__name__)
                logger_.warning("binding operation failed", exc_info=True)
            self._binding_op_done.emit()

        self._spawn(worker)

    def _on_binding_op_done(self) -> None:
        self._binding_busy = False
        self.refresh_automations()
        self.refresh()

    def toggle_binding(self, binding) -> None:
        self._run_binding_op(
            lambda: self._controller.set_binding_enabled(binding.binding_id, not binding.is_enabled),
            "Updating…",
        )

    def remove_binding(self, binding) -> None:
        confirm = QMessageBox.question(
            self,
            "Remove automation",
            f"Stop and remove the automation for “{binding.group_name}”?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._run_binding_op(
            lambda: self._controller.delete_binding(binding.binding_id),
            "Removing…",
        )

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
        # Sending drives WhatsApp to the foreground; return here once it's done
        # (whether it succeeded or not) so the user lands back on winSpark.
        self._return_to_front()

    def open_chat(self) -> None:
        chat = self.current_chat()
        if not chat:
            self._send_check.set_bad("Choose a chat first")
            return
        # Already the chat winSpark is showing? Then it's already open in
        # WhatsApp — there's nothing to do, so don't drive WhatsApp (which can
        # fail to RE-open an emoji-named chat and then wrongly report failure).
        from winspark.connectors.whatsapp_chat_name_rules import chat_names_match

        active = getattr(self, "_active_chat_name", "").strip()
        if active and (active.lower() == chat.lower() or chat_names_match(chat, active)):
            self._send_check.clear_status()
            self.refresh_messages()
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
        # "Open chat" foregrounds WhatsApp — bring winSpark back once it's done.
        self._return_to_front()

    def _return_to_front(self) -> None:
        """Raise winSpark back above WhatsApp after an action that foregrounded
        it (Send / Open chat), so the user isn't left staring at WhatsApp."""
        window = self.window()
        if window is not None:
            window.raise_()
            window.activateWindow()

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
        # Remember which chat is actually open in WhatsApp — that's the chat
        # winSpark reads and stores memory under (its title may be a phone
        # number even when you selected a saved name), so the memory view
        # below needs it to find the right memory.
        self._active_chat_name = (active or "").strip()
        if not messages:
            self._messages_view.setPlainText("")
            self._messages_status.setText(
                "No messages yet — press “Open chat” to load this chat in WhatsApp."
                if self.current_chat()
                else ""
            )
            self._refresh_memory_view()
            return
        lines = [f"{'You' if not m.is_incoming else (m.sender or 'Them')}:  {m.text}" for m in messages]
        self._messages_view.setPlainText("\n".join(lines))
        scrollbar = self._messages_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        if active:
            self._messages_status.setText(f"Showing “{active}” — updates every 3 seconds")
        else:
            self._messages_status.setText("Showing the chat currently open in WhatsApp")
        # A fresh read just synced this conversation into memory — reflect it.
        self._refresh_memory_view()

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
    _tabs_ready = Signal(int, object)      # (window_handle, [(title, is_current)])
    _ask_done = Signal(bool, str)
    _agent_progress = Signal(str)          # one line to append to the step log
    _agent_ask = Signal(str)               # a risky step needs approval (description)
    _agent_question = Signal(str)          # the agent is in doubt — ask the user
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
        # Cross-thread question handshake — when the agent is in doubt it asks
        # the user and blocks for a typed answer (None = user stopped).
        self._question_event = threading.Event()
        self._question_answer: Optional[str] = None
        self._question_pending = False   # agent is blocked waiting on an answer
        self._ask_user = self._ask_user_via_ui
        # Set by the Stop button; the loop checks it between rounds (a step
        # already in flight finishes — we never yank input mid-keystroke).
        self._agent_stop = threading.Event()
        # Free-text guidance the user types while the agent runs; the loop
        # drains it into the history before each step.
        self._guidance_lock = threading.Lock()
        self._pending_guidance: list[str] = []
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        self._tabs_seen_title = None   # last window title we read tabs for (throttle)
        self._tabs_busy = False
        self._ocr_done.connect(self._on_ocr_done)
        self._tabs_ready.connect(self._on_tabs_ready)
        self._ask_done.connect(self._on_ask_done)
        self._agent_progress.connect(self._on_agent_progress)
        self._agent_ask.connect(self._on_agent_ask)
        self._agent_question.connect(self._on_agent_question)
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

        # Select between the browser's open tabs (the window title only shows
        # the active one). Picking a tab switches the browser to it, so read /
        # ask / do then target that tab. Refreshes when the tab set changes.
        self._tab_row = QWidget()
        tab_row = QHBoxLayout(self._tab_row)
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.addWidget(QLabel("Tab:"))
        self._tab_combo = _NoWheelComboBox()
        _allow_narrow(self._tab_combo)
        self._tab_combo.activated.connect(self._on_tab_selected)  # fires on user pick only
        tab_row.addWidget(self._tab_combo, 1)
        self._tab_row.hide()
        layout.addWidget(self._tab_row)

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
        # One section for AI on this app, with a mode switch: "Ask about it"
        # reads the screen and answers a question; "Do something" runs the
        # acting agent (instruction → plan from the app's real controls →
        # approval for risky steps → execute). Same box, two jobs.
        interact_group = QGroupBox("Ask or act on this app")
        ig = QVBoxLayout(interact_group)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode"))
        self._interact_mode = _NoWheelComboBox()
        _allow_narrow(self._interact_mode)
        self._interact_mode.addItem("Ask about it", "ask")
        self._interact_mode.addItem("Do something", "act")
        self._interact_mode.currentIndexChanged.connect(self._on_interact_mode_changed)
        mode_row.addWidget(self._interact_mode, 1)
        ig.addLayout(mode_row)

        # --- Ask sub-panel ---
        self._ask_panel = QWidget()
        ag = QVBoxLayout(self._ask_panel)
        ag.setContentsMargins(0, 0, 0, 0)
        ask_hint = QLabel("winSpark reads what's on screen and answers your question.")
        ask_hint.setWordWrap(True)
        ask_hint.setStyleSheet("color: #64748b;")
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
        ig.addWidget(self._ask_panel)

        # --- Act sub-panel ---
        self._act_panel = QWidget()
        dg = QVBoxLayout(self._act_panel)
        dg.setContentsMargins(0, 0, 0, 0)
        act_hint = QLabel("Tell winSpark what to do and it will carry it out, step by step.")
        act_hint.setWordWrap(True)
        act_hint.setStyleSheet("color: #64748b;")
        dg.addWidget(act_hint)
        self._agent_input = QLineEdit()
        self._agent_input.setPlaceholderText("e.g. click Save, or: search for shoes")
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
        self._agent_view.setPlaceholderText("The steps winSpark takes (and what happened) will appear here.")
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
        # When the agent is unsure, it asks here and waits for your answer.
        question_row = QHBoxLayout()
        self._question_label = QLabel()
        self._question_label.setWordWrap(True)
        self._question_label.setStyleSheet("font-weight: 600;")
        self._answer_input = QLineEdit()
        self._answer_input.setPlaceholderText("Type your answer…")
        self._answer_input.returnPressed.connect(self._submit_answer)
        answer_btn = QPushButton("Answer")
        answer_btn.setObjectName("primary")
        answer_btn.clicked.connect(self._submit_answer)
        question_row.addWidget(self._answer_input, 1)
        question_row.addWidget(answer_btn)
        question_col = QVBoxLayout()
        question_col.setContentsMargins(0, 0, 0, 0)
        question_col.addWidget(self._question_label)
        question_col.addLayout(question_row)
        self._agent_question_box = QWidget()
        self._agent_question_box.setLayout(question_col)
        self._agent_question_box.hide()
        dg.addWidget(self._agent_question_box)
        # Steer the agent WHILE it works — type a nudge and it folds into the
        # next decision (e.g. "no, the other button", "use my work account").
        guide_row = QHBoxLayout()
        self._guide_input = QLineEdit()
        self._guide_input.setPlaceholderText("Add guidance while it runs (optional)…")
        self._guide_input.returnPressed.connect(self._send_guidance)
        self._guide_btn = QPushButton("Guide")
        self._guide_btn.clicked.connect(self._send_guidance)
        guide_row.addWidget(self._guide_input, 1)
        guide_row.addWidget(self._guide_btn)
        self._guide_box = QWidget()
        self._guide_box.setLayout(guide_row)
        self._guide_box.hide()  # only while a run is in progress
        dg.addWidget(self._guide_box)
        self._agent_check = StatusCheck()
        dg.addWidget(self._agent_check)
        self._act_panel.hide()  # Ask is the default mode
        ig.addWidget(self._act_panel)
        layout.addWidget(interact_group)

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
        self._watch_chat.setPlaceholderText("WhatsApp chat to message (contact name or phone number)")
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
        self._tabs_seen_title = None
        self._tab_row.hide()
        self.refresh_tabs()
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

    def _primary_window_title(self) -> str:
        """The selected window's real title (falls back to the app's primary
        title). Used to detect when you switched/opened a tab — browsers put the
        active page's title in the window title — so tab reads re-run only then."""
        handle = self._primary_handle()
        if self._app is None or handle is None:
            return ""
        for h, title in self._app.windows:
            if h == handle:
                return title or ""
        return self._app.primary_title or ""

    def refresh_tabs(self, force: bool = False) -> None:
        """Read the selected browser window's open tabs off the UI thread and
        show them. Throttled to when the window title changes (you switched or
        opened a tab) so it doesn't re-read every tick; `force` bypasses that
        (used on window selection)."""
        if not hasattr(self._controller, "list_browser_tabs"):
            return
        handle = self._primary_handle()
        if handle is None or self._tabs_busy:
            return
        title = self._primary_window_title()
        if not force and title == self._tabs_seen_title:
            return
        self._tabs_seen_title = title
        self._tabs_busy = True

        def worker():
            try:
                tabs = self._controller.list_browser_tabs(handle)
            except Exception:  # noqa: BLE001
                tabs = []
            self._tabs_ready.emit(handle, tabs)

        self._spawn(worker)

    def _on_tabs_ready(self, handle: int, tabs) -> None:
        self._tabs_busy = False
        if handle != self._primary_handle() or len(tabs) < 2:
            self._tab_row.hide()  # nothing to choose between
            return
        self._tab_combo.blockSignals(True)   # repopulating must not fire activate
        self._tab_combo.clear()
        current_index = 0
        for i, (name, is_current) in enumerate(tabs):
            self._tab_combo.addItem(name[:70], name)
            if is_current:
                current_index = i
        self._tab_combo.setCurrentIndex(current_index)
        self._tab_combo.blockSignals(False)
        self._tab_row.show()

    def _on_tab_selected(self, *_args) -> None:
        """User picked a tab — switch the browser to it, then re-target."""
        handle = self._primary_handle()
        title = self._tab_combo.currentData()
        if handle is None or not title or not hasattr(self._controller, "activate_browser_tab"):
            return

        def worker():
            self._controller.activate_browser_tab(handle, title)

        self._spawn(worker)
        self._clear_outputs()
        self._tabs_seen_title = None  # the active tab changed — re-read next tick

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
        self.refresh_tabs()  # a title changed -> the tab set likely did too

    def _on_window_changed(self, *_args) -> None:
        self._clear_outputs()
        self._tabs_seen_title = None
        self._tab_row.hide()
        self.refresh_tabs(force=True)

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

    def _on_interact_mode_changed(self, *_args) -> None:
        acting = self._interact_mode.currentData() == "act"
        self._ask_panel.setVisible(not acting)
        self._act_panel.setVisible(acting)

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
        with self._guidance_lock:
            self._pending_guidance = []
        self._do_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._agent_confirm.hide()
        self._agent_view.clear()
        self._guide_input.clear()
        self._guide_box.show()  # let the user steer while it runs
        self._agent_check.set_busy("Working on it, step by step…")
        app_name = self._app.display_name if self._app else ""

        def worker():
            from winspark.automation.screen_agent import describe_step

            history: list[str] = []
            last_description = None
            last_digest = None
            final_ok, final_message = False, ""
            stopped_by_user = "Stopped by you — nothing more was done."
            consecutive_failures = 0
            answered_questions: dict[str, str] = {}   # normalized question -> your answer
            question_repeats: dict[str, int] = {}
            rounds = 0
            # No fixed step budget: the loop cycles until the goal is done, the
            # user stops it, or the app stops responding. Every 15 rounds it
            # checks in with the user instead of silently grinding on.
            while True:
                rounds += 1
                if self._agent_stop.is_set():
                    final_message = stopped_by_user
                    break
                # Fold in any guidance the user typed since the last step — it
                # becomes part of what the agent sees when deciding the next one.
                # (already echoed to the log by _send_guidance when typed)
                for note in self._drain_guidance():
                    history.append(f"User guidance: {note}")
                if rounds % 15 == 0:
                    if not self._request_approval(f"{rounds - 1} steps in and not finished — keep going?"):
                        final_message = f"Stopped after {rounds - 1} steps — try breaking the request into smaller pieces."
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
                    try:
                        self._controller.remember_agent_success(app_name, instruction, history)
                    except Exception:  # noqa: BLE001
                        pass
                    break
                if decision.question:
                    # The agent is in doubt — ask the user instead of guessing.
                    # But never nag: a question that was already answered gets
                    # the same answer replayed instead of re-prompting, and a
                    # third repeat stops the run rather than looping.
                    key = decision.question.strip().lower()
                    if key in answered_questions:
                        question_repeats[key] = question_repeats.get(key, 0) + 1
                        if question_repeats[key] >= 2:
                            final_message = f"Stopped — it kept re-asking “{decision.question}” despite your answer."
                            break
                        history.append(
                            f"Asked you: {decision.question} -> you said: {answered_questions[key]}"
                            " (already answered — do NOT ask this again)"
                        )
                        self._agent_progress.emit("？ " + decision.question + "  (reusing your earlier answer)")
                        continue
                    self._agent_progress.emit("？ " + decision.question)
                    answer = self._ask_user(decision.question)
                    if answer is None:
                        final_message = stopped_by_user
                        break
                    answered_questions[key] = answer
                    history.append(f"Asked you: {decision.question} -> you said: {answer}")
                    self._agent_progress.emit("   ↳ you: " + answer)
                    continue

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
                if step_ok:
                    consecutive_failures = 0
                else:
                    # Fallback: the AI sees the failure next round and tries a
                    # different approach; after 3 misses in a row, ask the user
                    # instead of flailing.
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        answer = self._ask_user(
                            f"That kept failing ({message}). Any advice — or press Stop to give up?"
                        )
                        if answer is None:
                            final_message = message
                            break
                        history.append(f"Asked you for help -> you said: {answer}")
                        self._agent_progress.emit("   ↳ you: " + answer)
                        consecutive_failures = 0

            try:
                self._controller.record_agent_result(final_message or "Done.")
            except Exception:  # noqa: BLE001
                pass
            self._agent_finished.emit(final_ok, final_message)

        self._spawn(worker)

    def stop_agent(self) -> None:
        """Interrupt the running loop. The current step (if one is mid-flight)
        finishes; nothing further happens. Also unblocks a pending risky-step
        approval (as a decline) and a pending question (as no answer), so a
        Stop during either prompt stops too."""
        self._agent_stop.set()
        self._approval_decision = False
        self._approval_event.set()
        self._question_pending = False
        self._question_answer = None
        self._question_event.set()
        self._agent_confirm.hide()
        self._agent_question_box.hide()
        self._agent_check.set_busy("Stopping…")

    def _ask_user_via_ui(self, question: str) -> Optional[str]:
        """Called from the loop worker: show the agent's question and block
        until the user answers (None = stopped / no answer)."""
        self._question_event.clear()
        self._question_answer = None
        self._agent_question.emit(question)
        self._question_event.wait(timeout=600)  # unanswered = no answer
        return self._question_answer

    def _on_agent_question(self, question: str) -> None:
        self._question_pending = True
        self._question_label.setText(question)
        self._answer_input.clear()
        self._agent_question_box.show()
        self._answer_input.setFocus()
        self._agent_check.set_busy("winSpark needs your input to continue")

    def _send_guidance(self) -> None:
        """Queue a nudge for the running agent (or the answer field if it's
        actually waiting on a question)."""
        text = self._guide_input.text().strip()
        if not text:
            return
        self._guide_input.clear()
        if not self._agent_busy:
            return
        # If the agent is blocked on a question, the guide box doubles as the
        # answer — queuing it would just leave the agent waiting.
        if self._question_pending:
            self._answer_input.setText(text)
            self._submit_answer()
            return
        with self._guidance_lock:
            self._pending_guidance.append(text)
        self._agent_view.appendPlainText("💬 you: " + text)

    def _drain_guidance(self) -> list:
        with self._guidance_lock:
            notes, self._pending_guidance = self._pending_guidance, []
        return notes

    def _submit_answer(self) -> None:
        answer = self._answer_input.text().strip()
        if not answer:
            return
        self._question_pending = False
        self._agent_question_box.hide()
        self._agent_check.set_busy("Working on it, step by step…")
        self._question_answer = answer
        self._question_event.set()

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
        self._agent_question_box.hide()
        self._guide_box.hide()
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
    _run_question = Signal(str)      # the running agent is in doubt — ask here

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._editing_id = None          # None while creating; an id while editing
        self._busy = False
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        # So tests can drive the delete-confirmation without a real dialog.
        self._confirm_delete = self._confirm_delete_dialog
        self._confirm_bulk_delete = self._confirm_bulk_delete_dialog
        # Cross-thread question handshake (same shape as the Do-it box): the
        # run worker blocks on the event until the user types an answer.
        self._question_event = threading.Event()
        self._question_answer: Optional[str] = None
        self._ask_user = self._ask_user_via_ui
        self._run_done.connect(self._on_run_done)
        self._run_progress.connect(self._on_run_progress)
        self._run_question.connect(self._on_run_question)

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

        # Master off switch: turn EVERY automation off in one click (distinct
        # from Pause, which only holds automatic triggers — this disables them).
        self._all_off_btn = QPushButton("⏻  Turn all off")
        self._all_off_btn.setToolTip("Turn every automation off at once")
        self._all_off_btn.clicked.connect(self.turn_all_off)
        self._all_on_btn = QPushButton("Turn all on")
        self._all_on_btn.setToolTip("Turn every automation back on")
        self._all_on_btn.clicked.connect(self.turn_all_on)

        new_row = QHBoxLayout()
        self._new_btn = QPushButton("＋  New automation")
        self._new_btn.setObjectName("primary")
        self._new_btn.clicked.connect(self.new_automation)
        new_row.addWidget(self._new_btn)
        # Multi-select toggle: reveals a checkbox on each automation for bulk
        # turn-on / turn-off / delete.
        self._select_btn = QPushButton("☑  Select")
        self._select_btn.setCheckable(True)
        self._select_btn.setToolTip("Select several automations to act on at once")
        self._select_btn.toggled.connect(self._set_multiselect)
        new_row.addWidget(self._select_btn)
        # Filter: see all, only the active (on) ones, or only the off ones.
        new_row.addWidget(QLabel("Show:"))
        self._filter = _NoWheelComboBox()
        self._filter.addItem("All", "all")
        self._filter.addItem("Active only", "active")
        self._filter.addItem("Off only", "off")
        self._filter.currentIndexChanged.connect(lambda _=0: self.reload())
        new_row.addWidget(self._filter)
        new_row.addStretch(1)
        new_row.addWidget(self._all_on_btn)
        new_row.addWidget(self._all_off_btn)
        layout.addLayout(new_row)

        # Bulk-action bar for multi-select (hidden until "Select" is on).
        self._multiselect = False
        self._row_checks: list = []  # (automation_id, QCheckBox), rebuilt each reload
        self._bulk_bar = QWidget()
        bulk = QHBoxLayout(self._bulk_bar)
        bulk.setContentsMargins(0, 0, 0, 0)
        self._select_all = QCheckBox("Select all")
        self._select_all.toggled.connect(self._on_select_all)
        bulk.addWidget(self._select_all)
        bulk_on = QPushButton("Turn on")
        bulk_on.clicked.connect(lambda: self._bulk_set_enabled(True))
        bulk_off = QPushButton("Turn off")
        bulk_off.clicked.connect(lambda: self._bulk_set_enabled(False))
        bulk_delete = QPushButton("Delete")
        bulk_delete.clicked.connect(self._bulk_delete_selected)
        for b in (bulk_on, bulk_off, bulk_delete):
            bulk.addWidget(b)
        bulk.addStretch(1)
        self._bulk_bar.hide()
        layout.addWidget(self._bulk_bar)

        self._run_check = StatusCheck()
        layout.addWidget(self._run_check)
        # Live step-by-step log while a run is happening (hidden when idle).
        self._run_log = QPlainTextEdit()
        self._run_log.setReadOnly(True)
        self._run_log.setFixedHeight(90)
        self._run_log.hide()
        layout.addWidget(self._run_log)

        # When the running agent needs your input, it asks right here — no need
        # to abort and rerun from the app's Do-it box.
        self._question_label = QLabel()
        self._question_label.setWordWrap(True)
        self._question_label.setStyleSheet("font-weight: 600;")
        self._answer_input = QLineEdit()
        self._answer_input.setPlaceholderText("Type your answer…")
        self._answer_input.returnPressed.connect(self._submit_answer)
        answer_btn = QPushButton("Answer")
        answer_btn.setObjectName("primary")
        answer_btn.clicked.connect(self._submit_answer)
        answer_row = QHBoxLayout()
        answer_row.addWidget(self._answer_input, 1)
        answer_row.addWidget(answer_btn)
        question_col = QVBoxLayout()
        question_col.setContentsMargins(0, 0, 0, 0)
        question_col.addWidget(self._question_label)
        question_col.addLayout(answer_row)
        self._question_box = QWidget()
        self._question_box.setLayout(question_col)
        self._question_box.hide()
        layout.addWidget(self._question_box)

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
        wa.addWidget(QLabel("Chat (contact name or phone number)"))
        self._wa_chat = QLineEdit()
        self._wa_chat.setPlaceholderText("Contact name (as shown in WhatsApp) or phone number")
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
        # Show the create/edit form at the TOP (right under "New automation"),
        # not below the whole list, so creating one is visible without scrolling.
        layout.insertWidget(layout.indexOf(self._run_check), self._editor)
        layout.addStretch(1)

        self.reload()

    # --- listing --------------------------------------------------------

    def reload(self) -> None:
        """Rebuild the saved-automations list from the controller, honoring the
        Show filter (all / active / off)."""
        self._load_pause_state()
        while self._rows.count():
            item = self._rows.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._row_checks = []
        # Newest first: a just-created (or just-duplicated) automation shows at
        # the top of the list rather than scrolled off the bottom.
        automations = sorted(self._controller.get_automations(), key=lambda a: a.id or 0, reverse=True)
        automations = [a for a in automations if self._passes_filter(a)]
        self._empty_label.setText(self._empty_message())
        self._empty_label.setVisible(not automations)
        for automation in automations:
            self._rows.addWidget(self._build_row(automation))
        # Reset the header "select all" for the freshly built rows.
        self._select_all.blockSignals(True)
        self._select_all.setChecked(False)
        self._select_all.blockSignals(False)

    def _passes_filter(self, automation) -> bool:
        mode = self._filter.currentData()
        if mode == "active":
            return bool(automation.enabled)
        if mode == "off":
            return not automation.enabled
        return True

    def _empty_message(self) -> str:
        mode = self._filter.currentData()
        if mode == "active":
            return "No active automations."
        if mode == "off":
            return "No automations are off."
        return "No automations yet — create one above."

    def _build_row(self, automation) -> QWidget:
        from PySide6.QtWidgets import QCheckBox

        row = QFrame()
        row.setObjectName("card")
        row.setStyleSheet("QFrame#card { border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; }")
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 8, 10, 8)
        top = QHBoxLayout()
        select_check = QCheckBox()
        select_check.setVisible(self._multiselect)
        select_check.setToolTip("Select this automation")
        self._row_checks.append((automation.id, select_check))
        top.addWidget(select_check)
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

    # --- multi-select + master switches ---------------------------------

    def _set_multiselect(self, on: bool) -> None:
        """Show/hide per-row checkboxes and the bulk-action bar."""
        self._multiselect = on
        self._bulk_bar.setVisible(on)
        for _id, check in self._row_checks:
            check.setVisible(on)
        if not on:
            self._select_all.blockSignals(True)
            self._select_all.setChecked(False)
            self._select_all.blockSignals(False)
            for _id, check in self._row_checks:
                check.setChecked(False)

    def _on_select_all(self, checked: bool) -> None:
        for _id, check in self._row_checks:
            check.setChecked(checked)

    def _selected_ids(self) -> list:
        return [aid for aid, check in self._row_checks if check.isChecked() and aid is not None]

    def _bulk_set_enabled(self, enabled: bool) -> None:
        for aid in self._selected_ids():
            self._controller.set_automation_enabled(aid, enabled)
        self.reload()

    def _bulk_delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if not self._confirm_bulk_delete(len(ids)):
            return
        for aid in ids:
            self._controller.delete_automation(aid)
        self.reload()

    def _confirm_bulk_delete_dialog(self, count: int) -> bool:
        answer = QMessageBox.question(
            self, "Delete automations?",
            f"Delete {count} selected automation{'s' if count != 1 else ''}? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def turn_all_off(self) -> None:
        """The single off switch: disable every automation at once."""
        self._controller.set_all_automations_enabled(False)
        self.reload()

    def turn_all_on(self) -> None:
        self._controller.set_all_automations_enabled(True)
        self.reload()

    def duplicate_automation(self, automation) -> None:
        """Save a copy as a brand-new (manual, so a copied schedule doesn't
        quietly start firing) automation the user can then tweak. Names it
        "<name> (1)", "(2)", … — the next free number for that base name."""
        existing = {a.name for a in self._controller.get_automations()}
        self._controller.save_automation(
            None, _next_duplicate_name(automation.name, existing), automation.kind,
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
                    automation_id,
                    progress=lambda line: self._run_progress.emit(line),
                    ask_user=self._ask_user,   # a manual run is attended — questions come here
                )
            except Exception as ex:  # noqa: BLE001
                ok, message = False, str(ex)
            self._run_done.emit(automation_id, ok, message)

        self._spawn(worker)

    def _ask_user_via_ui(self, question: str) -> Optional[str]:
        """Called from the run worker: surface the agent's question and block
        until the user answers (None = no answer / gave up waiting)."""
        self._question_event.clear()
        self._question_answer = None
        self._run_question.emit(question)
        self._question_event.wait(timeout=600)
        return self._question_answer

    def _on_run_question(self, question: str) -> None:
        self._question_label.setText(question)
        self._answer_input.clear()
        self._question_box.show()
        self._answer_input.setFocus()
        self._run_check.set_busy("winSpark needs your input to continue")
        # The run pushed the target app in front of winSpark — bring the window
        # back so the question is actually seen.
        window = self.window()
        if window is not None:
            window.raise_()
            window.activateWindow()

    def _submit_answer(self) -> None:
        answer = self._answer_input.text().strip()
        if not answer:
            return
        self._question_box.hide()
        self._run_check.set_busy("Continuing…")
        self._question_answer = answer
        self._question_event.set()

    def _on_run_progress(self, line: str) -> None:
        self._run_log.appendPlainText(line)

    def _on_run_done(self, automation_id: int, ok: bool, message: str) -> None:
        self._busy = False
        self._question_box.hide()   # in case the run ended at a question
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

    # (backend, migrated, problem, database) — a MongoDB connect finished.
    _mongo_done = Signal(str, int, str, str)

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        self._mongo_busy = False
        self._mongo_done.connect(self._on_mongo_done)

        # Scroll the content: with the AI + chat-memory sections stacked, the
        # panel is taller than a short window, and a plain layout compresses
        # the top fields below their real height (seen live: clipped text).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout.addWidget(QLabel("<h2>Settings</h2>"))

        ai_group = QGroupBox("AI")
        _ai_outer = QVBoxLayout(ai_group)
        _ai_body = QWidget()
        ag = QVBoxLayout(_ai_body)
        ag.setContentsMargins(0, 0, 0, 0)
        _ai_outer.addWidget(_ai_body)
        _collapsible(ai_group, _ai_body, start_open=True)

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
        self._key.setPlaceholderText("API key")
        self._key.textChanged.connect(lambda _: self._check.clear_status())
        ag.addWidget(self._key)

        self._model = QLineEdit()
        self._model.setToolTip("Model — leave as is unless you know you want another")
        ag.addWidget(self._model)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Style:"))
        self._style = _NoWheelComboBox()
        _allow_narrow(self._style)
        self._style.addItem("Precise", "precise")
        self._style.addItem("Balanced", "balanced")
        self._style.addItem("Creative", "creative")
        self._style.currentIndexChanged.connect(self._on_style_changed)
        style_row.addWidget(self._style, 1)
        ag.addLayout(style_row)

        from PySide6.QtWidgets import QCheckBox

        self._web_search = QCheckBox("Look things up on the web")
        self._web_search.toggled.connect(self._on_web_search_toggled)
        ag.addWidget(self._web_search)

        buttons = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self.save)
        test_btn = QPushButton("Test")
        test_btn.clicked.connect(self.test_connection)
        buttons.addWidget(save_btn)
        buttons.addWidget(test_btn)
        buttons.addStretch(1)
        ag.addLayout(buttons)

        self._check = StatusCheck()
        ag.addWidget(self._check)
        layout.addWidget(ai_group)

        # --- Storage (optional MongoDB) --------------------------------------
        if hasattr(self._controller, "get_chat_memory_mongo_uri"):
            mem_group = QGroupBox("Storage")
            _mem_outer = QVBoxLayout(mem_group)
            _mem_body = QWidget()
            mem = QVBoxLayout(_mem_body)
            mem.setContentsMargins(0, 0, 0, 0)
            _mem_outer.addWidget(_mem_body)
            _collapsible(mem_group, _mem_body, start_open=False)
            mem_hint = QLabel(
                "Everything is kept on this PC. Paste a MongoDB link to keep chats "
                "and messages in your own database too."
            )
            mem_hint.setWordWrap(True)
            mem_hint.setStyleSheet("color: #64748b;")
            mem.addWidget(mem_hint)
            self._mongo_uri = QLineEdit()
            self._mongo_uri.setPlaceholderText("mongodb://…  or  mongodb+srv://…")
            mem.addWidget(self._mongo_uri)
            self._mongo_db = QLineEdit()
            self._mongo_db.setPlaceholderText(f"Database ({DEFAULT_CHAT_MEMORY_MONGO_DB})")
            self._mongo_db.setToolTip("A database named in the link itself wins over this.")
            mem.addWidget(self._mongo_db)
            mem_save = QPushButton("Save & connect")
            mem_save.setObjectName("primary")
            mem_save.clicked.connect(self.save_chat_memory)
            mem_btns = QHBoxLayout()
            mem_btns.addWidget(mem_save)
            mem_btns.addStretch(1)
            mem.addLayout(mem_btns)
            self._mongo_check = StatusCheck()
            mem.addWidget(self._mongo_check)
            layout.addWidget(mem_group)
            self._storage_card = mem_group

        self._ai_card = ai_group
        layout.addStretch(1)

        self.reload()

    def reload(self) -> None:
        """Refresh fields from the saved settings (called when the panel opens)."""
        self._provider.blockSignals(True)
        self._provider.setCurrentIndex(max(0, self._provider.findData(self._controller.get_ai_provider())))
        self._provider.blockSignals(False)
        self._key.setText(self._controller.get_openai_api_key())
        self._model.setText(self._controller.get_openai_model())
        self._style.blockSignals(True)
        self._style.setCurrentIndex(max(0, self._style.findData(self._controller.get_ai_style())))
        self._style.blockSignals(False)
        self._web_search.blockSignals(True)
        self._web_search.setChecked(bool(self._controller.get_ai_web_search()))
        self._web_search.blockSignals(False)
        self._on_provider_changed()
        self._check.clear_status()
        if hasattr(self, "_mongo_uri"):
            self._mongo_uri.setText(self._controller.get_chat_memory_mongo_uri())
            self._last_mongo_state = None      # force a re-render on reopen
            self.refresh_chat_memory_status()

    def _mongo_state(self) -> tuple:
        """(backend, problem, offline, pending, database) — everything the status
        line depends on, so a redraw can be skipped when none of it moved."""
        return (
            self._controller.chat_memory_backend(),
            getattr(self._controller, "chat_memory_last_error", lambda: "")(),
            bool(getattr(self._controller, "chat_memory_offline", lambda: False)()),
            int(getattr(self._controller, "chat_memory_pending_writes", lambda: 0)()),
            getattr(self._controller, "chat_memory_database", lambda: "")(),
        )

    def refresh_chat_memory_status(self) -> None:
        """Update the chat-memory status line. Cheap (in-memory flags only), so
        the main window can call it on its refresh timer — MongoDB going away
        mid-session is otherwise invisible unless you read the log.

        Only redraws when something actually changed, so it can't wipe out the
        "Connected. Moved N messages over." confirmation a moment after a save."""
        if not hasattr(self, "_mongo_uri"):
            return
        backend, problem, offline, pending, database = state = self._mongo_state()
        if state == getattr(self, "_last_mongo_state", None):
            return
        self._last_mongo_state = state

        if problem or offline:
            # A collapsed card must never hide a live failure — pop it open so
            # the ✗ is actually seen.
            if hasattr(self, "_storage_card"):
                self._storage_card.setChecked(True)
        if problem:
            # A URI is saved but MongoDB was unreachable at startup — say so on
            # sight, rather than letting it look like local storage was the
            # choice the user made.
            self._mongo_check.set_bad(f"{problem} Using local storage for now.")
        elif offline:
            waiting = (f" {pending} message{'s' if pending != 1 else ''} will go over when it's back."
                       if pending else "")
            self._mongo_check.set_bad(
                "MongoDB isn't answering — saving to local storage meanwhile." + waiting)
        else:
            where = f" (database: {database})" if database else ""
            self._mongo_check.set_busy(f"Currently using {backend}{where}.")

    def save_chat_memory(self) -> None:
        """Persist the MongoDB settings and reconnect now, reporting whether the
        connection actually took (MongoDB) or fell back to local storage.

        Connecting runs on a worker thread: reaching an Atlas cluster means an
        SRV lookup plus a TLS handshake over the internet, so this can take
        seconds — long enough to visibly freeze the window if done inline."""
        if self._mongo_busy:
            return
        uri = self._mongo_uri.text().strip()
        database = self._mongo_db.text().strip()
        self._mongo_busy = True
        self._mongo_check.set_busy("Connecting…" if uri else "Switching to local storage…")

        def worker():
            backend, migrated, problem, db_in_use = "local storage", 0, "", ""
            try:
                backend, migrated = self._controller.set_chat_memory_mongo(uri, database)
                # Optional on the controller — absent on older/fake controllers.
                problem = getattr(self._controller, "chat_memory_last_error", lambda: "")()
                db_in_use = getattr(self._controller, "chat_memory_database", lambda: "")()
            except Exception as ex:  # noqa: BLE001 - report it, never kill the thread
                problem = str(ex)
            self._mongo_done.emit(backend, migrated, problem, db_in_use)

        self._spawn(worker)

    def _on_mongo_done(self, backend: str, migrated: int, problem: str, database: str) -> None:
        self._mongo_busy = False
        # Record the state this confirmation reflects, so the refresh timer sees
        # nothing changed and leaves the "Moved N messages over" message up.
        self._last_mongo_state = self._mongo_state()
        if not self._mongo_uri.text().strip():
            self._mongo_check.set_ok("Using local storage.")
        elif backend == "MongoDB":
            moved = f" Moved {migrated} remembered message{'s' if migrated != 1 else ''} over." if migrated else ""
            where = f" (database: {database})" if database else ""
            self._mongo_check.set_ok(f"Connected to MongoDB{where}." + moved)
        else:
            # Say what's actually wrong — "couldn't reach MongoDB" is useless
            # when the real cause is an IP allowlist or a wrong password.
            self._mongo_check.set_bad(
                (problem or "Couldn't reach MongoDB.") + " Using local storage for now."
            )

    def _on_style_changed(self, *_args) -> None:
        self._controller.set_ai_style(self._style.currentData())

    def _on_web_search_toggled(self, checked: bool) -> None:
        self._controller.set_ai_web_search(checked)

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
    """The plain-English activity feed, with a colored Passed/Failed badge
    beside each line so outcomes are readable at a glance."""

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("<b>Activity</b>"))
        self._table = make_table(["When", "Result", "What happened"], stretch_col=2)
        self._table.setColumnWidth(0, 74)
        self._table.setColumnWidth(1, 66)
        layout.addWidget(self._table)

    def refresh(self) -> None:
        from PySide6.QtGui import QColor

        from winspark.ui.activity import outcome_color, outcome_label

        entries = self._controller.get_activity_log(200)
        self._table.setRowCount(len(entries))
        for r, entry in enumerate(entries):
            # Tolerate the old 2-tuple shape as well as (when, text, outcome).
            when, text = entry[0], entry[1]
            outcome = entry[2] if len(entry) > 2 else "info"
            when_item = QTableWidgetItem(when.strftime("%H:%M:%S"))
            result_item = QTableWidgetItem(outcome_label(outcome))
            result_item.setForeground(QColor(outcome_color(outcome)))
            result_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(r, 0, when_item)
            self._table.setItem(r, 1, result_item)
            self._table.setItem(r, 2, QTableWidgetItem(text))
