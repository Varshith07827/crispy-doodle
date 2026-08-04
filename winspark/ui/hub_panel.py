"""The Message Hub panel — the two flows, kept visibly separate.

    Send      Refresh list → pick a chat → paste a webhook → it polls and sends
    Save      Refresh list → pick a chat → its messages go to MongoDB

One chat picker at the top serves both, because that's the only thing they
share. Below it the two flows are separate boxes that can be used in either
order, or one without the other — the point of the redesign. Nothing here is a
three-step wizard: pick a chat, then switch on whichever half you want.

Everything slow (loading chats, connecting to MongoDB, sending) runs on a
worker thread and reports back through a signal, so the window never freezes —
the same pattern the other panels use.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from winspark.hub.settings_files import DEFAULT_COLLECTION, MIN_SPOOL_SECONDS
from winspark.ui.panels import _allow_narrow, _NoWheelComboBox
from winspark.ui.widgets import StatusCheck

_INTERVALS = ((3, "every 3 seconds"), (5, "every 5 seconds"), (15, "every 15 seconds"),
              (30, "every 30 seconds"), (60, "every minute"), (300, "every 5 minutes"))


class HubPanel(QWidget):
    """Send + Save, over `config.json`/`data.json` and MongoDB."""

    _chats_ready = Signal(object)          # [chat names] or None when unavailable
    _mongo_done = Signal(bool, str)        # (connected, message)
    _link_done = Signal()                  # a link/unlink finished — refresh

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._spawn = lambda worker: threading.Thread(target=worker, daemon=True).start()
        self._busy = False
        self._chats_ready.connect(self._on_chats_ready)
        self._mongo_done.connect(self._on_mongo_done)
        self._link_done.connect(self.refresh)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        title = QLabel("Message Hub")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)
        intro = QLabel(
            "Pick a chat, then switch on either half. Sending and saving are "
            "independent — you can do one without the other."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #64748b;")
        layout.addWidget(intro)

        layout.addWidget(self._build_chat_box())
        layout.addWidget(self._build_send_box())
        layout.addWidget(self._build_save_box())
        layout.addWidget(self._build_mongo_box())
        layout.addStretch(1)

        self.reload()

    # --- construction ---------------------------------------------------

    def _build_chat_box(self) -> QGroupBox:
        box = QGroupBox("Chat")
        row = QHBoxLayout(box)
        self._refresh_button = QPushButton("Refresh list")
        self._refresh_button.clicked.connect(self.refresh_chats)
        row.addWidget(self._refresh_button)
        self._chat_combo = _NoWheelComboBox()
        _allow_narrow(self._chat_combo)
        self._chat_combo.setEditable(True)
        self._chat_combo.currentTextChanged.connect(self._on_chat_changed)
        row.addWidget(self._chat_combo, 1)
        self._chat_check = StatusCheck()
        row.addWidget(self._chat_check)
        return box

    def _build_send_box(self) -> QGroupBox:
        box = QGroupBox("Send messages to this chat")
        col = QVBoxLayout(box)
        hint = QLabel(
            "winSpark checks the web address below on a timer and sends anything "
            "it returns to the chat. Leave it empty to send nothing."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #64748b;")
        col.addWidget(hint)

        col.addWidget(QLabel("Web address to check"))
        self._webhook = QLineEdit()
        self._webhook.setPlaceholderText("https://example.com/my-webhook")
        col.addWidget(self._webhook)

        row = QHBoxLayout()
        row.addWidget(QLabel("Check:"))
        self._interval = _NoWheelComboBox()
        _allow_narrow(self._interval)
        for seconds, label in _INTERVALS:
            self._interval.addItem(label, seconds)
        row.addWidget(self._interval, 1)
        self._send_button = QPushButton("Link & start")
        self._send_button.setObjectName("primary")
        self._send_button.clicked.connect(self.toggle_send)
        row.addWidget(self._send_button)
        self._unlink_button = QPushButton("Unlink")
        self._unlink_button.clicked.connect(self.unlink_send)
        row.addWidget(self._unlink_button)
        col.addLayout(row)

        self._send_status = StatusCheck()
        col.addWidget(self._send_status)
        return box

    def _build_save_box(self) -> QGroupBox:
        box = QGroupBox("Save this chat's messages")
        col = QVBoxLayout(box)
        hint = QLabel(
            "Messages are written straight to MongoDB as they arrive — there is no "
            "local copy, so anything that fails to save is reported rather than kept. "
            "Only the chat currently open in WhatsApp can be read."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #64748b;")
        col.addWidget(hint)

        self._capture = QCheckBox("Save this chat's messages to MongoDB")
        self._capture.toggled.connect(self._on_capture_toggled)
        col.addWidget(self._capture)
        self._save_status = StatusCheck()
        col.addWidget(self._save_status)
        return box

    def _build_mongo_box(self) -> QGroupBox:
        box = QGroupBox("MongoDB")
        col = QVBoxLayout(box)
        col.addWidget(QLabel("Connection string"))
        self._mongo_uri = QLineEdit()
        self._mongo_uri.setPlaceholderText("mongodb://user:pass@host:27017/database")
        col.addWidget(self._mongo_uri)

        row = QHBoxLayout()
        row.addWidget(QLabel("Collection:"))
        self._collection = QLineEdit()
        self._collection.setPlaceholderText(DEFAULT_COLLECTION)
        self._collection.setToolTip(
            "The collection every message is written to. Defaults to "
            f"{DEFAULT_COLLECTION!r} — deliberately not 'chat_memory', which "
            "belongs to the older AI-memory feature."
        )
        row.addWidget(self._collection, 1)
        self._connect_button = QPushButton("Save & connect")
        self._connect_button.setObjectName("primary")
        self._connect_button.clicked.connect(self.save_mongo)
        row.addWidget(self._connect_button)
        col.addLayout(row)

        self._mongo_status = StatusCheck()
        col.addWidget(self._mongo_status)
        return box

    # --- state ----------------------------------------------------------

    def current_chat(self) -> str:
        return self._chat_combo.currentText().strip()

    def selected_interval(self) -> int:
        return int(self._interval.currentData() or MIN_SPOOL_SECONDS)

    def reload(self) -> None:
        """Fill the fields from config.json / data.json."""
        config = self._controller.hub_config()
        self._mongo_uri.setText(config.mongo_uri)
        self._collection.setText(config.mongo_collection)
        self.refresh()

    def refresh(self) -> None:
        """Update everything that depends on the selected chat. Cheap — reads
        in-memory settings only, so the main window can call it on its timer."""
        chat = self.current_chat()
        link = self._controller.hub_send_link(chat) if chat else None

        self._webhook.setText(link.webhook_url if link else "")
        if link is not None:
            index = self._interval.findData(link.interval_seconds)
            if index >= 0:
                self._interval.setCurrentIndex(index)
        running = bool(link and link.enabled)
        self._send_button.setText("Stop" if running else "Link & start")
        self._unlink_button.setEnabled(link is not None)

        self._capture.blockSignals(True)
        self._capture.setChecked(bool(chat) and self._controller.hub_is_capturing(chat))
        self._capture.blockSignals(False)

        for widget in (self._webhook, self._interval, self._send_button, self._capture):
            widget.setEnabled(bool(chat))

        self._render_status(chat)

    def _render_status(self, chat: str) -> None:
        if not chat:
            self._send_status.set_busy("Pick a chat first.")
            self._save_status.set_busy("Pick a chat first.")
            return
        send_line = self._controller.hub_spool_status(chat)
        link = self._controller.hub_send_link(chat)
        if link and link.enabled:
            self._send_status.set_ok(send_line)
        else:
            self._send_status.set_busy(send_line)

        save_line = self._controller.hub_capture_status()
        if self._controller.hub_is_capturing(chat):
            self._save_status.set_ok(save_line)
        else:
            self._save_status.set_busy(save_line)

    # --- chats ----------------------------------------------------------

    def refresh_chats(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._refresh_button.setEnabled(False)
        self._chat_check.set_busy("Reading your chats…")

        def worker():
            try:
                chats = self._controller.get_whatsapp_chats()
            except Exception:  # noqa: BLE001 - reported through the signal
                chats = None
            self._chats_ready.emit(chats)

        self._spawn(worker)

    def _on_chats_ready(self, chats) -> None:
        self._busy = False
        self._refresh_button.setEnabled(True)
        if chats is None:
            self._chat_check.set_bad("WhatsApp isn't available.")
            return
        names = [getattr(c, "chat_name", str(c)) for c in chats]
        names = [n for n in names if (n or "").strip()]
        keep = self.current_chat()
        self._chat_combo.blockSignals(True)
        self._chat_combo.clear()
        self._chat_combo.addItems(names)
        if keep in names:
            self._chat_combo.setCurrentIndex(names.index(keep))
        self._chat_combo.blockSignals(False)
        self._chat_check.set_ok(f"{len(names)} chat{'s' if len(names) != 1 else ''}")
        self.refresh()

    def _on_chat_changed(self, *_args) -> None:
        self.refresh()

    # --- send flow ------------------------------------------------------

    def toggle_send(self) -> None:
        chat = self.current_chat()
        if not chat:
            self._chat_check.set_bad("Pick a chat first")
            return
        link = self._controller.hub_send_link(chat)
        starting = not (link and link.enabled)
        url = self._webhook.text().strip()
        if starting and not url:
            self._send_status.set_bad("Paste a web address to check first.")
            return
        interval = self.selected_interval()

        def worker():
            try:
                self._controller.hub_set_send_link(chat, url, starting, interval)
            except Exception:  # noqa: BLE001 - refresh shows the real state
                pass
            self._link_done.emit()

        self._spawn(worker)

    def unlink_send(self) -> None:
        chat = self.current_chat()
        if not chat:
            return

        def worker():
            try:
                self._controller.hub_remove_send_link(chat)
            except Exception:  # noqa: BLE001
                pass
            self._link_done.emit()

        self._spawn(worker)

    # --- save flow ------------------------------------------------------

    def _on_capture_toggled(self, checked: bool) -> None:
        chat = self.current_chat()
        if not chat:
            return
        self._controller.hub_set_capture(chat, checked)
        self.refresh()

    # --- MongoDB --------------------------------------------------------

    def save_mongo(self) -> None:
        uri = self._mongo_uri.text().strip()
        collection = self._collection.text().strip() or DEFAULT_COLLECTION
        self._connect_button.setEnabled(False)
        self._mongo_status.set_busy("Connecting…")

        def worker():
            try:
                ok, message = self._controller.hub_save_mongo(uri, collection)
            except Exception as ex:  # noqa: BLE001
                ok, message = False, str(ex)
            self._mongo_done.emit(ok, message)

        self._spawn(worker)

    def _on_mongo_done(self, ok: bool, message: str) -> None:
        self._connect_button.setEnabled(True)
        if ok:
            self._mongo_status.set_ok(message)
        else:
            self._mongo_status.set_bad(message)
        self.refresh()
