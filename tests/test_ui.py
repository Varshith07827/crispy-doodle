"""Headless tests for the product UI: the guided WhatsApp panel, the generic
observe-only panel, the activity log, and the app-sidebar shell.

Runs Qt under the 'offscreen' platform and drives each panel's logic against a
fake controller — no real engine, WhatsApp, display, or STA thread.
"""

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from winspark.automation.screen_agent import PlanStep, StepDecision  # noqa: E402
from winspark.connectors.fetch_webhook_models import WhatsAppFetchBindingEntity  # noqa: E402
from winspark.connectors.models import WhatsAppChatRow  # noqa: E402
from winspark.connectors.screen_watch import ScreenWatcherEntity  # noqa: E402
from winspark.connectors.whatsapp import WhatsAppMessage  # noqa: E402
from winspark.domain.models import WindowInfo  # noqa: E402
from winspark.ui.apps import detect_running_apps  # noqa: E402


def _step(action="click", name="Save", risky=False, text=""):
    return PlanStep(action=action, control_index=0, control_type="ButtonControl",
                    control_name=name, risky=risky, text=text)


def _decision(step=None, done=False, summary="", digest="d1"):
    return StepDecision(done=done, summary=summary, step=step, screen_digest=digest)


class FakeController:
    def __init__(self):
        self.relay_enabled = False
        self.started: list[tuple[str, str, int]] = []
        self.stopped: list[str] = []
        self.tested_sources: list[str] = []
        self.chats_available = True
        self.chats = [
            WhatsAppChatRow(chat_name="Family", timestamp_text="", last_message="hi", unread_count=2, raw_text="Family"),
            WhatsAppChatRow(chat_name="Work", timestamp_text="", last_message="ok", unread_count=0, raw_text="Work"),
        ]
        self.findable = {"Family", "Work"}
        self.source_ok = True
        self.openai_key = ""
        self.openai_model = "gpt-4o-mini"
        self.ai_provider = "openai"
        self.openai_ok = True
        self.sent_messages: list[tuple[str, str]] = []
        self.opened_chats: list[str] = []
        self.send_ok = True
        self.recent_messages = [
            WhatsAppMessage(sender="Family", text="dinner at 8?", is_incoming=True),
            WhatsAppMessage(sender="You", text="sounds good", is_incoming=False),
        ]
        self.active_conversation = "Family"
        self.screen_text = "Invoice #42\nTotal: $100"
        self.screen_image = None  # PNG bytes; set by tests that verify the preview
        self.ocr_ok = True
        self.ask_ok = True
        self._running_chats: set[str] = set()
        self.windows = [
            WindowInfo(handle=1, title="WhatsApp", process_name="WhatsApp.Root.exe", is_active=True),
            WindowInfo(handle=2, title="Untitled - Notepad", process_name="notepad.exe"),
        ]
        self.activity = [(datetime.now(timezone.utc), "Automation started — watching for new messages")]
        self.bindings = [
            WhatsAppFetchBindingEntity(binding_id="b1", group_name="Family", reply_source="web", is_enabled=True),
            WhatsAppFetchBindingEntity(
                binding_id="b2", group_name="Ma", reply_source="trigger",
                trigger_text="Good morning", is_enabled=False,
            ),
        ]
        self.toggled: list[tuple[str, bool]] = []
        self.deleted: list[str] = []
        self.watchers: list[ScreenWatcherEntity] = []
        self.added_watchers: list[dict] = []
        self.watcher_toggled: list[tuple[str, bool]] = []
        self.watcher_deleted: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.agent_mode = "ask_risky"
        self.agent_script: list = [
            _decision(step=_step("click", "Save")),
            _decision(done=True, summary="Saved the file."),
        ]
        self.agent_execute_result = (True, "Click “Save”")
        self.agent_steps_executed: list = []
        self.agent_next_calls: list = []
        self.agent_summaries: list = []

    # apps / status / activity
    def get_running_apps(self):
        return detect_running_apps(self.windows)

    def is_relay_enabled(self):
        return self.relay_enabled

    def get_activity_log(self, limit=200):
        return list(self.activity)[:limit]

    # whatsapp
    def get_whatsapp_chats(self):
        return list(self.chats) if self.chats_available else None

    def can_find_chat(self, chat):
        return chat in self.findable

    def test_message_source(self, url, chat):
        self.tested_sources.append(url)
        return (self.source_ok, "Connected" if self.source_ok else "the website returned an error")

    def is_chat_automation_running(self, chat):
        return self.relay_enabled and chat in self._running_chats

    # automations list
    def get_bindings(self):
        return list(self.bindings)

    def set_binding_enabled(self, binding_id, enabled):
        self.toggled.append((binding_id, enabled))
        for i, b in enumerate(self.bindings):
            if b.binding_id == binding_id:
                from dataclasses import replace

                self.bindings[i] = replace(b, is_enabled=enabled)

    def delete_binding(self, binding_id):
        self.deleted.append(binding_id)
        self.bindings = [b for b in self.bindings if b.binding_id != binding_id]

    # screen watchers
    def get_watchers(self):
        return list(self.watchers)

    def add_watcher(self, process_name, title_hint, display_name, watch_text,
                    action_kind="notify", whatsapp_chat="", whatsapp_message="", interval=10):
        self.added_watchers.append({
            "process_name": process_name, "title_hint": title_hint, "display_name": display_name,
            "watch_text": watch_text, "action_kind": action_kind, "whatsapp_chat": whatsapp_chat,
            "whatsapp_message": whatsapp_message, "interval": interval,
        })
        self.watchers.append(ScreenWatcherEntity(
            process_name=process_name, app_display_name=display_name, watch_text=watch_text,
            action_kind=action_kind, whatsapp_chat=whatsapp_chat, status="watching",
        ))

    def set_watcher_enabled(self, watcher_id, enabled):
        self.watcher_toggled.append((watcher_id, enabled))

    def delete_watcher(self, watcher_id):
        self.watcher_deleted.append(watcher_id)
        self.watchers = [w for w in self.watchers if w.watcher_id != watcher_id]

    def pop_notifications(self):
        items, self.notifications = list(self.notifications), []
        return items

    # the "Do it" agent (closed loop)
    def get_agent_mode(self):
        return self.agent_mode

    def set_agent_mode(self, mode):
        self.agent_mode = mode

    def agent_next_step(self, window_handle, app_name, goal, history):
        self.agent_next_calls.append((window_handle, app_name, goal, tuple(history)))
        if not self.agent_script:
            return (False, "script exhausted")
        item = self.agent_script.pop(0)
        return item if isinstance(item, tuple) else (True, item)

    def agent_execute_step(self, window_handle, step):
        self.agent_steps_executed.append(step)
        return self.agent_execute_result

    def record_agent_result(self, summary):
        self.agent_summaries.append(summary)

    # openai (app-wide)
    def get_openai_api_key(self):
        return self.openai_key

    def get_openai_model(self):
        return self.openai_model

    def get_ai_provider(self):
        return self.ai_provider

    def set_openai_config(self, api_key, model="", provider=""):
        self.openai_key = api_key
        self.openai_model = model or "gpt-4o-mini"
        if provider:
            self.ai_provider = provider

    def test_openai_connection(self):
        return (self.openai_ok, "Connected to OpenAI" if self.openai_ok else "OpenAI rejected the key — check that it's correct.")

    # messages (send + live view)
    def send_to_chat(self, chat, text):
        self.sent_messages.append((chat, text))
        return (self.send_ok, "sent" if self.send_ok else "Couldn't reach WhatsApp")

    def open_chat(self, chat):
        self.opened_chats.append(chat)
        return True

    def get_recent_messages(self, limit=15):
        return (self.active_conversation, list(self.recent_messages)[:limit])

    # ocr (generic apps)
    def read_screen_text(self, window_handle):
        self.ocr_handle = window_handle
        return (self.ocr_ok, self.screen_text if self.ocr_ok else "No readable text was found on that window.")

    def capture_screen_image(self, window_handle):
        return self.screen_image

    def ask_about_screen(self, window_handle, question):
        self.asked = (window_handle, question)
        return (self.ask_ok, "It shows invoice #42 for $100." if self.ask_ok else "No AI key set — add it first.")

    def start_chat_automation(
        self, chat, url, interval, reply_source="web", ai_mode="reply", ai_prompt="",
        trigger_text="", reply_text="",
    ):
        self.started.append((chat, url, interval))
        self.last_start_kwargs = {
            "reply_source": reply_source, "ai_mode": ai_mode, "ai_prompt": ai_prompt,
            "trigger_text": trigger_text, "reply_text": reply_text,
        }
        self.relay_enabled = True
        self._running_chats.add(chat)

    def stop_chat_automation(self, chat):
        self.stopped.append(chat)
        self._running_chats.discard(chat)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller():
    return FakeController()


# --- WhatsApp guided panel -------------------------------------------------

def _run_inline(panel):
    """Make the panel's background work run synchronously so tests can assert
    right after calling an action (production runs it on a worker thread)."""
    panel._spawn = lambda worker: worker()
    return panel


@pytest.fixture
def whatsapp(qapp, controller):
    from winspark.ui.panels import WhatsAppPanel

    panel = _run_inline(WhatsAppPanel(controller))
    panel.refresh_chats()  # chats now load on selection, not construction
    return panel


def _select_openai(panel):
    panel._method_combo.setCurrentIndex(panel._method_combo.findData("openai"))


def test_chats_populate_the_list(whatsapp):
    """Ported from the original .NET app's design: an always-visible
    QListWidget of recent chats, not a combo box popup — see
    WhatsAppCommandPanel.xaml / MainViewModel.WhatsAppAutomation.cs. No popup
    to open means nothing that can fail to open."""
    assert whatsapp._chat_list.count() == 2
    assert whatsapp._chat_list.item(0).text() == "Family"


def test_clicking_a_chat_in_the_list_fills_the_name_field(whatsapp):
    """Ported from OnSelectedWhatsAppChatChanged: picking a chat from the list
    just copies its name into the text field."""
    item = whatsapp._chat_list.item(1)
    whatsapp._chat_list.itemClicked.emit(item)

    assert whatsapp.current_chat() == "Work"


def test_refresh_chats_repopulates_the_list(whatsapp, controller):
    controller.chats = list(controller.chats) + [
        controller.chats[0].__class__(chat_name="New Chat", timestamp_text="", last_message="", unread_count=0, raw_text="New Chat")
    ]

    whatsapp.refresh_chats()

    assert whatsapp._chat_list.count() == 3
    assert whatsapp._chat_list.item(2).text() == "New Chat"


# --- automations list (see what's running, pause/remove) -------------------

def test_automations_table_lists_existing_bindings(whatsapp, controller):
    table = whatsapp._automations_table
    assert table.rowCount() == 2
    assert table.item(0, 0).text() == "Family"
    assert table.item(0, 2).text() == "Running"
    assert table.item(1, 0).text() == "Ma"
    assert "Good morning" in table.item(1, 1).text()
    assert table.item(1, 2).text() == "Paused"


def test_automations_table_hidden_when_no_bindings(whatsapp, controller):
    controller.bindings = []
    whatsapp.refresh_automations()
    assert whatsapp._automations_table.isHidden() is True
    assert whatsapp._automations_empty_label.isHidden() is False


def test_toggle_binding_pauses_a_running_automation(whatsapp, controller):
    running = controller.bindings[0]
    assert running.is_enabled is True

    whatsapp.toggle_binding(running)

    assert controller.toggled == [("b1", False)]


def test_toggle_binding_resumes_a_paused_automation(whatsapp, controller):
    paused = controller.bindings[1]
    assert paused.is_enabled is False

    whatsapp.toggle_binding(paused)

    assert controller.toggled == [("b2", True)]


def test_remove_binding_asks_for_confirmation_then_deletes(qapp, whatsapp, controller, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    whatsapp.remove_binding(controller.bindings[0])

    assert controller.deleted == ["b1"]


def test_remove_binding_cancelled_does_not_delete(qapp, whatsapp, controller, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))

    whatsapp.remove_binding(controller.bindings[0])

    assert controller.deleted == []


def test_check_chat_shows_green_when_found(whatsapp):
    whatsapp._chat_name.setText("Family")
    whatsapp.check_chat()
    assert whatsapp._chat_check.state == "ok"
    assert "Found" in whatsapp._chat_check.message


def test_check_chat_shows_red_when_not_found(whatsapp):
    whatsapp._chat_name.setText("Nonexistent")
    whatsapp.check_chat()
    assert whatsapp._chat_check.state == "bad"


def test_test_source_shows_connected(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    whatsapp.test_source()
    assert whatsapp._source_check.state == "ok"
    assert controller.tested_sources == [""]  # blank -> built-in source


def test_test_source_shows_failure_in_plain_english(whatsapp, controller):
    controller.source_ok = False
    whatsapp._chat_name.setText("Family")
    whatsapp.test_source()
    assert whatsapp._source_check.state == "bad"
    assert "HTTP" not in whatsapp._source_check.message


def test_check_interval_options():
    # 3s is the default (first) option.
    from winspark.ui.panels import _CHECK_INTERVALS

    assert _CHECK_INTERVALS[0] == ("Every 3 seconds", 3)


def test_scrolling_over_a_dropdown_does_not_change_the_option(qapp, whatsapp):
    """The panels scroll; the mouse wheel passing over a dropdown must scroll
    the page, not silently change the selection (Qt's default does the latter).
    A plain QComboBox flips its index on this exact event — the panels'
    dropdowns must not."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QComboBox

    def wheel_down(widget):
        event = QWheelEvent(
            QPointF(5, 5), widget.mapToGlobal(QPointF(5, 5)), QPoint(0, 0), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(widget, event)
        return event

    # Sanity: prove the event we synthesize DOES change a default combo box,
    # so the assertion below is actually testing the override.
    plain = QComboBox()
    plain.addItems(["a", "b"])
    wheel_down(plain)
    assert plain.currentIndex() == 1

    for combo in (whatsapp._method_combo, whatsapp._interval_combo, whatsapp._ai_mode, whatsapp._ai_provider):
        combo.setCurrentIndex(0)
        event = wheel_down(combo)
        assert combo.currentIndex() == 0, f"{combo} changed on wheel"
        assert not event.isAccepted()  # bubbles up so the page still scrolls


def test_start_and_stop_automation_for_the_chosen_chat(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    whatsapp._interval_combo.setCurrentIndex(1)  # every 5 seconds

    whatsapp.toggle_automation()
    assert controller.started == [("Family", "", 5)]
    assert whatsapp.is_running() is True
    assert whatsapp._start_button.text() == "Stop automation"

    whatsapp.toggle_automation()
    assert controller.stopped == ["Family"]
    assert whatsapp._start_button.text() == "Start automation"


def test_openai_method_shows_key_and_prompt_fields(whatsapp):
    # Default method is the web/test source; the AI panel is hidden.
    assert whatsapp.current_reply_source() == "web"
    assert whatsapp._ai_panel.isVisible() is False

    _select_openai(whatsapp)
    assert whatsapp.current_reply_source() == "openai"
    assert whatsapp._web_panel.isVisible() is False


def test_test_connection_uses_openai_when_selected(whatsapp, controller):
    _select_openai(whatsapp)
    whatsapp._ai_key.setText("sk-test-123")
    whatsapp.test_source()
    assert controller.openai_key == "sk-test-123"
    assert whatsapp._source_check.state == "ok"


def test_openai_test_connection_shows_plain_failure(whatsapp, controller):
    controller.openai_ok = False
    _select_openai(whatsapp)
    whatsapp._ai_key.setText("bad")
    whatsapp.test_source()
    assert whatsapp._source_check.state == "bad"
    assert "HTTP" not in whatsapp._source_check.message


def test_start_with_openai_passes_prompt_and_mode(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    _select_openai(whatsapp)
    whatsapp._ai_key.setText("sk-abc")
    whatsapp._ai_prompt.setPlainText("Reply kindly.")
    whatsapp.toggle_automation()

    assert controller.started == [("Family", "", 3)]
    assert controller.openai_key == "sk-abc"
    assert controller.last_start_kwargs["reply_source"] == "openai"
    assert controller.last_start_kwargs["ai_mode"] == "reply"  # reply is the default
    assert controller.last_start_kwargs["ai_prompt"] == "Reply kindly."


def _select_method(panel, key):
    panel._method_combo.setCurrentIndex(panel._method_combo.findData(key))


def test_trigger_method_shows_wait_and_reply_fields(whatsapp):
    _select_method(whatsapp, "trigger")
    assert whatsapp.current_reply_source() == "trigger"
    # isVisible() is always False when the top-level window isn't shown (headless),
    # so check the explicit hidden state set by setVisible instead.
    assert whatsapp._trigger_panel.isHidden() is False
    assert whatsapp._web_panel.isHidden() is True
    assert whatsapp._ai_panel.isHidden() is True


def test_start_trigger_passes_wait_and_reply(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    _select_method(whatsapp, "trigger")
    whatsapp._trigger_text.setText("asking if I'm coming")
    whatsapp._trigger_reply.setPlainText("Yes, on my way!")
    whatsapp.toggle_automation()

    assert controller.started == [("Family", "", 3)]
    assert controller.last_start_kwargs["reply_source"] == "trigger"
    assert controller.last_start_kwargs["trigger_text"] == "asking if I'm coming"
    assert controller.last_start_kwargs["reply_text"] == "Yes, on my way!"


def test_ai_provider_can_be_switched_to_groq(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    _select_openai(whatsapp)
    whatsapp._ai_provider.setCurrentIndex(whatsapp._ai_provider.findData("groq"))
    whatsapp._ai_key.setText("gsk-abc")
    whatsapp.toggle_automation()

    assert controller.ai_provider == "groq"
    assert controller.openai_key == "gsk-abc"
    assert controller.last_start_kwargs["reply_source"] == "openai"


def test_openai_generate_mode_can_be_selected(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    _select_openai(whatsapp)
    whatsapp._ai_mode.setCurrentIndex(whatsapp._ai_mode.findData("generate"))
    whatsapp._ai_key.setText("sk-abc")
    whatsapp.toggle_automation()

    assert controller.last_start_kwargs["ai_mode"] == "generate"


def test_send_message_to_selected_chat(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    whatsapp._compose.setText("hello there")
    whatsapp.send_message()
    assert controller.sent_messages == [("Family", "hello there")]
    assert whatsapp._compose.text() == ""  # cleared after sending
    assert whatsapp._send_check.state == "ok"


def test_send_message_without_a_chat_is_guarded(whatsapp, controller):
    whatsapp._chat_name.setText("")
    whatsapp._compose.setText("hi")
    whatsapp.send_message()
    assert controller.sent_messages == []
    assert whatsapp._send_check.state == "bad"


def test_send_failure_shows_plain_reason(whatsapp, controller):
    controller.send_ok = False
    whatsapp._chat_name.setText("Family")
    whatsapp._compose.setText("hi")
    whatsapp.send_message()
    assert whatsapp._send_check.state == "bad"
    assert whatsapp._compose.text() == "hi"  # kept so the user can retry


def test_open_chat_button_opens_selected_chat(whatsapp, controller):
    whatsapp._chat_name.setText("Work")
    whatsapp.open_chat()
    assert controller.opened_chats == ["Work"]


def test_recent_messages_populate_the_view(whatsapp):
    whatsapp._chat_name.setText("Family")
    whatsapp.refresh_messages()
    text = whatsapp._messages_view.toPlainText()
    assert "Family:  dinner at 8?" in text
    assert "You:  sounds good" in text
    assert "Family" in whatsapp._messages_status.text()


def test_start_without_a_chat_is_guarded(whatsapp, controller):
    whatsapp._chat_name.setText("")
    whatsapp.toggle_automation()
    assert controller.started == []
    assert whatsapp._chat_check.state == "bad"


def test_whatsapp_unavailable_disables_chat_input(qapp, controller):
    from winspark.ui.panels import WhatsAppPanel

    controller.chats_available = False
    panel = _run_inline(WhatsAppPanel(controller))
    panel.refresh_chats()
    assert panel._chat_list.isEnabled() is False


# --- generic + activity panels --------------------------------------------

def test_generic_panel_describes_an_observe_only_app(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    apps = detect_running_apps(controller.windows)
    notepad = next(a for a in apps if a.display_name == "Notepad")
    panel = GenericAppPanel(controller)
    panel.set_app(notepad)
    assert "Notepad" in panel._title.text()
    assert "can't automate this app yet" in panel._body.text()


def test_generic_panel_reads_screen_text_with_ocr(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    apps = detect_running_apps(controller.windows)
    notepad = next(a for a in apps if a.display_name == "Notepad")
    panel = _run_inline(GenericAppPanel(controller))
    panel.set_app(notepad)

    panel.read_text()
    assert "Invoice #42" in panel._ocr_view.toPlainText()
    assert panel._copy_btn.isEnabled() is True
    assert controller.ocr_handle in notepad.window_handles


def _tiny_png() -> bytes:
    """A real 8x8 PNG generated in-process — no fixture files."""
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#14b8a6"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def test_read_text_shows_the_captured_screenshot(qapp, controller):
    controller.screen_image = _tiny_png()
    panel = _generic_panel_with_notepad(controller)

    panel.read_text()

    assert panel._shot_label.isHidden() is False
    assert panel._shot_label.pixmap() is not None
    assert not panel._shot_label.pixmap().isNull()


def test_read_text_without_screenshot_keeps_preview_hidden(qapp, controller):
    controller.screen_image = None
    panel = _generic_panel_with_notepad(controller)

    panel.read_text()

    assert panel._shot_label.isHidden() is True
    assert "Invoice #42" in panel._ocr_view.toPlainText()  # text still works


def test_generic_panel_ask_ai_answers_about_the_screen(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    apps = detect_running_apps(controller.windows)
    notepad = next(a for a in apps if a.display_name == "Notepad")
    panel = _run_inline(GenericAppPanel(controller))
    panel.set_app(notepad)

    panel._question.setText("What's the total?")
    panel.ask_ai()
    assert "invoice #42" in panel._answer_view.toPlainText().lower()
    assert controller.asked[0] in notepad.window_handles
    assert controller.asked[1] == "What's the total?"


def test_generic_panel_ask_ai_failure_shows_plain_message(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    controller.ask_ok = False
    apps = detect_running_apps(controller.windows)
    notepad = next(a for a in apps if a.display_name == "Notepad")
    panel = _run_inline(GenericAppPanel(controller))
    panel.set_app(notepad)

    panel._question.setText("What's the total?")
    panel.ask_ai()
    assert panel._answer_view.toPlainText() == ""
    assert "AI key" in panel._ask_status.text()


# --- multiple windows of the same app ----------------------------------------

def _controller_with_two_notepads():
    controller = FakeController()
    controller.windows = [
        WindowInfo(handle=1, title="WhatsApp", process_name="WhatsApp.Root.exe", is_active=True),
        WindowInfo(handle=2, title="notes.txt - Notepad", process_name="notepad.exe"),
        WindowInfo(handle=3, title="todo.txt - Notepad", process_name="notepad.exe"),
    ]
    return controller


def test_window_picker_hidden_for_single_window_app(qapp, controller):
    panel = _generic_panel_with_notepad(controller)
    assert panel._window_row.isHidden() is True


def test_window_picker_lists_and_switches_windows(qapp):
    controller = _controller_with_two_notepads()
    panel = _generic_panel_with_notepad(controller)

    assert panel._window_row.isHidden() is False
    assert panel._window_combo.count() == 2
    assert panel._window_combo.itemText(0) == "notes.txt - Notepad"
    assert panel._primary_handle() == 2

    panel._window_combo.setCurrentIndex(1)
    assert panel._primary_handle() == 3

    panel.read_text()
    assert controller.ocr_handle == 3  # actions target the chosen window


def test_switching_window_clears_stale_outputs(qapp):
    controller = _controller_with_two_notepads()
    panel = _generic_panel_with_notepad(controller)
    panel.read_text()
    assert panel._ocr_view.toPlainText() != ""

    panel._window_combo.setCurrentIndex(1)
    assert panel._ocr_view.toPlainText() == ""  # old result was for the other window


def test_watcher_targets_the_selected_window_by_title(qapp):
    controller = _controller_with_two_notepads()
    panel = _generic_panel_with_notepad(controller)
    panel._window_combo.setCurrentIndex(1)
    panel._watch_text.setText("done")
    panel.start_watching()

    assert controller.added_watchers[0]["title_hint"] == "todo.txt - Notepad"


def test_watcher_hint_empty_for_single_window_app(qapp, controller):
    panel = _generic_panel_with_notepad(controller)
    panel._watch_text.setText("done")
    panel.start_watching()
    assert controller.added_watchers[0]["title_hint"] == ""


def test_update_app_windows_adds_new_window_and_keeps_selection(qapp):
    controller = _controller_with_two_notepads()
    panel = _generic_panel_with_notepad(controller)
    panel._window_combo.setCurrentIndex(1)  # choose todo.txt (handle 3)

    controller.windows.append(WindowInfo(handle=4, title="draft.txt - Notepad", process_name="notepad.exe"))
    fresh = next(a for a in detect_running_apps(controller.windows) if a.display_name == "Notepad")
    panel.update_app_windows(fresh)

    assert panel._window_combo.count() == 3
    assert panel._primary_handle() == 3  # selection preserved across the refresh


# --- the "Do it" agent (closed loop: act, look, act) -------------------------

def test_agent_loop_executes_then_stops_when_ai_says_done(qapp, controller):
    panel = _generic_panel_with_notepad(controller)
    panel._agent_input.setText("save the file")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 1
    assert panel._agent_check.state == "ok"
    assert "Saved the file." in panel._agent_check.message
    # THE point of the closed loop: the second AI turn saw the REAL result of
    # the first step, not an assumption.
    assert controller.agent_next_calls[1][3] == ("Click “Save” -> ok",)


def test_agent_loop_reobserves_between_steps(qapp, controller):
    controller.agent_script = [
        _decision(step=_step("click", "Open"), digest="screen-1"),
        _decision(step=_step("type", "Search", text="shoes"), digest="screen-2"),
        _decision(done=True, summary="Searched."),
    ]
    panel = _generic_panel_with_notepad(controller)
    panel._agent_input.setText("search for shoes")
    panel.do_it()

    assert len(controller.agent_next_calls) == 3  # looked at the app before every decision
    assert len(controller.agent_steps_executed) == 2
    assert panel._agent_check.state == "ok"


def test_risky_step_waits_for_approval_in_ask_mode(qapp, controller):
    controller.agent_mode = "ask_risky"
    controller.agent_script = [
        _decision(step=_step("click", "Send", risky=True)),
        _decision(done=True, summary="Sent."),
    ]
    asked = []
    panel = _generic_panel_with_notepad(controller)
    panel._request_approval = lambda description: (asked.append(description), True)[1]
    panel._agent_input.setText("send the message")
    panel.do_it()

    assert asked == ["Click “Send”"]
    assert len(controller.agent_steps_executed) == 1


def test_risky_step_declined_stops_the_loop(qapp, controller):
    controller.agent_script = [_decision(step=_step("click", "Delete", risky=True))]
    panel = _generic_panel_with_notepad(controller)
    panel._request_approval = lambda description: False
    panel._agent_input.setText("delete it")
    panel.do_it()

    assert controller.agent_steps_executed == []
    assert panel._agent_check.state == "bad"
    assert "risky" in panel._agent_check.message


def test_risky_step_runs_without_asking_in_auto_mode(qapp, controller):
    controller.agent_mode = "auto"
    controller.agent_script = [
        _decision(step=_step("click", "Send", risky=True)),
        _decision(done=True, summary="Sent."),
    ]
    panel = _generic_panel_with_notepad(controller)
    panel._request_approval = lambda description: (_ for _ in ()).throw(AssertionError("must not ask in auto mode"))
    panel._agent_input.setText("send it")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 1
    assert panel._agent_check.state == "ok"


def test_failed_step_stops_the_loop_and_reports(qapp, controller):
    controller.agent_script = [
        _decision(step=_step("click", "Save")),
        _decision(step=_step("click", "Close")),
    ]
    controller.agent_execute_result = (False, "Couldn’t find “Save” anymore")
    panel = _generic_panel_with_notepad(controller)
    panel._agent_input.setText("save then close")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 1  # stopped, didn't barrel on
    assert panel._agent_check.state == "bad"


def test_repeated_step_on_unchanged_screen_aborts(qapp, controller):
    same = _step("click", "Load more")
    controller.agent_script = [
        _decision(step=same, digest="frozen"),
        _decision(step=same, digest="frozen"),  # same step, same screen -> stuck
    ]
    panel = _generic_panel_with_notepad(controller)
    panel._agent_input.setText("load everything")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 1
    assert "didn’t respond" in panel._agent_check.message or "didn't respond" in panel._agent_check.message


def test_loop_round_budget_caps_runaway_goals(qapp, controller):
    controller.agent_script = [
        _decision(step=_step("click", f"Next {i}"), digest=f"d{i}") for i in range(20)
    ]
    panel = _generic_panel_with_notepad(controller)
    panel._agent_input.setText("click next forever")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 8
    assert "8 steps" in panel._agent_check.message


def test_next_step_failure_shows_plain_error(qapp, controller):
    controller.agent_script = [(False, "No AI key set — add it first.")]
    panel = _generic_panel_with_notepad(controller)
    panel._agent_input.setText("do something")
    panel.do_it()

    assert controller.agent_steps_executed == []
    assert "AI key" in panel._agent_check.message


def test_changing_agent_mode_persists_via_controller(qapp, controller):
    panel = _generic_panel_with_notepad(controller)
    panel._agent_mode.setCurrentIndex(panel._agent_mode.findData("auto"))
    assert controller.agent_mode == "auto"


# --- screen watchers (watch any app) ----------------------------------------

def _generic_panel_with_notepad(controller):
    from winspark.ui.panels import GenericAppPanel

    apps = detect_running_apps(controller.windows)
    notepad = next(a for a in apps if a.display_name == "Notepad")
    panel = _run_inline(GenericAppPanel(controller))
    panel.set_app(notepad)
    return panel


def test_start_watching_adds_a_watcher_for_the_selected_app(qapp, controller):
    panel = _generic_panel_with_notepad(controller)
    panel._watch_text.setText("Download complete")
    panel.start_watching()

    assert len(controller.added_watchers) == 1
    added = controller.added_watchers[0]
    assert added["process_name"] == "notepad.exe"
    assert added["display_name"] == "Notepad"
    assert added["watch_text"] == "Download complete"
    assert added["action_kind"] == "notify"
    assert panel._watch_check.state == "ok"


def test_start_watching_requires_watch_text(qapp, controller):
    panel = _generic_panel_with_notepad(controller)
    panel.start_watching()
    assert controller.added_watchers == []
    assert panel._watch_check.state == "bad"


def test_start_watching_whatsapp_action_requires_a_chat(qapp, controller):
    panel = _generic_panel_with_notepad(controller)
    panel._watch_text.setText("Out for delivery")
    panel._watch_action.setCurrentIndex(panel._watch_action.findData("whatsapp"))
    panel.start_watching()
    assert controller.added_watchers == []
    assert panel._watch_check.state == "bad"

    panel._watch_chat.setText("Family")
    panel.start_watching()
    assert len(controller.added_watchers) == 1
    assert controller.added_watchers[0]["action_kind"] == "whatsapp"
    assert controller.added_watchers[0]["whatsapp_chat"] == "Family"


def test_watchers_table_lists_all_watchers_with_status(qapp, controller):
    controller.watchers = [
        ScreenWatcherEntity(app_display_name="Chrome", watch_text="Download complete", status="watching"),
        ScreenWatcherEntity(app_display_name="Code", watch_text="build finished", is_enabled=False, status="matched"),
    ]
    panel = _generic_panel_with_notepad(controller)

    table = panel._watchers_table
    assert table.rowCount() == 2
    assert table.item(0, 0).text() == "Chrome"
    assert table.item(0, 3).text() == "Watching"
    assert table.item(1, 3).text() == "Found it ✓"


def test_toggle_and_remove_watcher(qapp, controller, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    watcher = ScreenWatcherEntity(app_display_name="Chrome", watch_text="done")
    controller.watchers = [watcher]
    panel = _generic_panel_with_notepad(controller)

    panel.toggle_watcher(watcher)
    assert controller.watcher_toggled == [(watcher.watcher_id, False)]

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    panel.remove_watcher(watcher)
    assert controller.watcher_deleted == [watcher.watcher_id]


def test_generic_panel_ocr_failure_shows_plain_message(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    controller.ocr_ok = False
    apps = detect_running_apps(controller.windows)
    notepad = next(a for a in apps if a.display_name == "Notepad")
    panel = _run_inline(GenericAppPanel(controller))
    panel.set_app(notepad)

    panel.read_text()
    assert panel._ocr_view.toPlainText() == ""
    assert panel._copy_btn.isEnabled() is False
    assert "No readable text" in panel._ocr_status.text()


def test_activity_panel_lists_plain_english(qapp, controller):
    from winspark.ui.panels import ActivityLogPanel

    panel = ActivityLogPanel(controller)
    panel.refresh()
    assert panel._table.rowCount() == 1
    assert "Automation started" in panel._table.item(0, 1).text()


# --- main window shell -----------------------------------------------------

@pytest.fixture
def window(qapp, controller):
    from winspark.ui.main_window import MainWindow

    win = MainWindow(controller)
    _run_inline(win._whatsapp_panel)  # selecting WhatsApp triggers a message read
    try:
        yield win
    finally:
        win.close()


def test_sidebar_lists_running_apps_supported_first(window):
    assert window._sidebar.count() == 2
    # WhatsApp (supported) is first, marked with a dot.
    assert "WhatsApp" in window._sidebar.item(0).text()
    assert window._sidebar.item(0).text().strip().startswith("●")


def test_selecting_whatsapp_shows_the_guided_panel(window):
    window._sidebar.setCurrentRow(0)
    assert window._stack.currentWidget() is window._whatsapp_panel


def test_selecting_unsupported_app_shows_generic_panel(window):
    for i in range(window._sidebar.count()):
        if "Notepad" in window._sidebar.item(i).text():
            window._sidebar.setCurrentRow(i)
            break
    assert window._stack.currentWidget() is window._generic_panel


def test_status_bar_summarizes_apps_and_automation(window, controller):
    window.refresh()
    msg = window.statusBar().currentMessage()
    assert "apps open" in msg
    assert "Automation: off" in msg


def test_sidebar_can_be_hidden_and_shown(window):
    assert window._left.isHidden() is False
    window._sidebar_toggle.setChecked(False)
    assert window._left.isHidden() is True
    window._sidebar_toggle.setChecked(True)
    assert window._left.isHidden() is False


def test_activity_panel_can_be_hidden_and_shown(window):
    assert window._activity_panel.isHidden() is False
    window._activity_toggle.setChecked(False)
    assert window._activity_panel.isHidden() is True
    window._activity_toggle.setChecked(True)
    assert window._activity_panel.isHidden() is False
