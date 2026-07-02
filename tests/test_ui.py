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

from winspark.connectors.models import WhatsAppChatRow  # noqa: E402
from winspark.connectors.whatsapp import WhatsAppMessage  # noqa: E402
from winspark.domain.models import WindowInfo  # noqa: E402
from winspark.ui.apps import detect_running_apps  # noqa: E402


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
        self.openai_ok = True
        self.sent_messages: list[tuple[str, str]] = []
        self.opened_chats: list[str] = []
        self.send_ok = True
        self.recent_messages = [
            WhatsAppMessage(sender="Family", text="dinner at 8?", is_incoming=True),
            WhatsAppMessage(sender="You", text="sounds good", is_incoming=False),
        ]
        self.active_conversation = "Family"
        self._running_chats: set[str] = set()
        self.windows = [
            WindowInfo(handle=1, title="WhatsApp", process_name="WhatsApp.Root.exe", is_active=True),
            WindowInfo(handle=2, title="Untitled - Notepad", process_name="notepad.exe"),
        ]
        self.activity = [(datetime.now(timezone.utc), "Automation started — watching for new messages")]

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

    # openai (app-wide)
    def get_openai_api_key(self):
        return self.openai_key

    def get_openai_model(self):
        return self.openai_model

    def set_openai_config(self, api_key, model=""):
        self.openai_key = api_key
        self.openai_model = model or "gpt-4o-mini"

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

    def start_chat_automation(self, chat, url, interval, reply_source="web", ai_mode="reply", ai_prompt=""):
        self.started.append((chat, url, interval))
        self.last_start_kwargs = {"reply_source": reply_source, "ai_mode": ai_mode, "ai_prompt": ai_prompt}
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

@pytest.fixture
def whatsapp(qapp, controller):
    from winspark.ui.panels import WhatsAppPanel

    return WhatsAppPanel(controller)


def _select_openai(panel):
    panel._method_combo.setCurrentIndex(panel._method_combo.findData("openai"))


def test_chats_populate_the_dropdown(whatsapp):
    assert whatsapp._chat_combo.count() == 2
    assert whatsapp._chat_combo.itemText(0) == "Family"


def test_check_chat_shows_green_when_found(whatsapp):
    whatsapp._chat_combo.setEditText("Family")
    whatsapp.check_chat()
    assert whatsapp._chat_check.state == "ok"
    assert "Found" in whatsapp._chat_check.message


def test_check_chat_shows_red_when_not_found(whatsapp):
    whatsapp._chat_combo.setEditText("Nonexistent")
    whatsapp.check_chat()
    assert whatsapp._chat_check.state == "bad"


def test_test_source_shows_connected(whatsapp, controller):
    whatsapp._chat_combo.setEditText("Family")
    whatsapp.test_source()
    assert whatsapp._source_check.state == "ok"
    assert controller.tested_sources == [""]  # blank -> built-in source


def test_test_source_shows_failure_in_plain_english(whatsapp, controller):
    controller.source_ok = False
    whatsapp._chat_combo.setEditText("Family")
    whatsapp.test_source()
    assert whatsapp._source_check.state == "bad"
    assert "HTTP" not in whatsapp._source_check.message


def test_check_interval_options():
    # 3s is the default (first) option.
    from winspark.ui.panels import _CHECK_INTERVALS

    assert _CHECK_INTERVALS[0] == ("Every 3 seconds", 3)


def test_start_and_stop_automation_for_the_chosen_chat(whatsapp, controller):
    whatsapp._chat_combo.setEditText("Family")
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
    whatsapp._chat_combo.setEditText("Family")
    _select_openai(whatsapp)
    whatsapp._ai_key.setText("sk-abc")
    whatsapp._ai_prompt.setPlainText("Reply kindly.")
    whatsapp.toggle_automation()

    assert controller.started == [("Family", "", 3)]
    assert controller.openai_key == "sk-abc"
    assert controller.last_start_kwargs["reply_source"] == "openai"
    assert controller.last_start_kwargs["ai_mode"] == "reply"  # reply is the default
    assert controller.last_start_kwargs["ai_prompt"] == "Reply kindly."


def test_openai_generate_mode_can_be_selected(whatsapp, controller):
    whatsapp._chat_combo.setEditText("Family")
    _select_openai(whatsapp)
    whatsapp._ai_mode.setCurrentIndex(whatsapp._ai_mode.findData("generate"))
    whatsapp._ai_key.setText("sk-abc")
    whatsapp.toggle_automation()

    assert controller.last_start_kwargs["ai_mode"] == "generate"


def test_send_message_to_selected_chat(whatsapp, controller):
    whatsapp._chat_combo.setEditText("Family")
    whatsapp._compose.setText("hello there")
    whatsapp.send_message()
    assert controller.sent_messages == [("Family", "hello there")]
    assert whatsapp._compose.text() == ""  # cleared after sending
    assert whatsapp._send_check.state == "ok"


def test_send_message_without_a_chat_is_guarded(whatsapp, controller):
    whatsapp._chat_combo.setEditText("")
    whatsapp._compose.setText("hi")
    whatsapp.send_message()
    assert controller.sent_messages == []
    assert whatsapp._send_check.state == "bad"


def test_send_failure_shows_plain_reason(whatsapp, controller):
    controller.send_ok = False
    whatsapp._chat_combo.setEditText("Family")
    whatsapp._compose.setText("hi")
    whatsapp.send_message()
    assert whatsapp._send_check.state == "bad"
    assert whatsapp._compose.text() == "hi"  # kept so the user can retry


def test_open_chat_button_opens_selected_chat(whatsapp, controller):
    whatsapp._chat_combo.setEditText("Work")
    whatsapp.open_chat()
    assert controller.opened_chats == ["Work"]


def test_recent_messages_populate_the_view(whatsapp):
    whatsapp._chat_combo.setEditText("Family")
    whatsapp.refresh_messages()
    text = whatsapp._messages_view.toPlainText()
    assert "Family:  dinner at 8?" in text
    assert "You:  sounds good" in text
    assert "Family" in whatsapp._messages_status.text()


def test_start_without_a_chat_is_guarded(whatsapp, controller):
    whatsapp._chat_combo.setEditText("")
    whatsapp.toggle_automation()
    assert controller.started == []
    assert whatsapp._chat_check.state == "bad"


def test_whatsapp_unavailable_disables_chat_input(qapp, controller):
    from winspark.ui.panels import WhatsAppPanel

    controller.chats_available = False
    panel = WhatsAppPanel(controller)
    assert panel._chat_combo.isEnabled() is False


# --- generic + activity panels --------------------------------------------

def test_generic_panel_describes_an_observe_only_app(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    apps = detect_running_apps(controller.windows)
    notepad = next(a for a in apps if a.display_name == "Notepad")
    panel = GenericAppPanel(controller)
    panel.set_app(notepad)
    assert "Notepad" in panel._title.text()
    assert "can't automate it yet" in panel._body.text()


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
