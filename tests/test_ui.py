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
        self.provider_keys: dict[str, str] = {}
        self.provider_models: dict[str, str] = {}
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
        self._running_chats: set[tuple[str, str]] = set()   # (chat, reply_source)
        self.windows = [
            WindowInfo(handle=1, title="WhatsApp", process_name="WhatsApp.Root.exe", is_active=True),
            WindowInfo(handle=2, title="Untitled - Notepad", process_name="notepad.exe"),
        ]
        self.activity = [
            (datetime.now(timezone.utc), "Sent the message to Family", "ok"),
            (datetime.now(timezone.utc), "Automation started — watching for new messages", "info"),
        ]
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
        self.remembered_successes: list = []
        self.ai_style = "precise"
        self.ai_web_search = True
        self.webhook_testing = True
        self.webhook_pending = 0
        self.browser_tabs = []
        self.activated_tabs = []
        self.automations: list = []
        self.saved_automations: list = []
        self.saved_triggers: list = []
        self.automation_enabled_calls: list = []
        self.deleted_automations: list = []
        self.ran_automations: list = []
        self.run_result = (True, "Done.")
        self.run_progress_lines: list = ["→ Click “Search”", "   ✓ Click “Search”"]
        self.run_question = ""       # set to make run_automation ask mid-run
        self.run_answer = None
        self.chat_memory = {}        # chat -> list[(role, sender, text)]
        self.chat_memory_backend_name = "local storage"
        self.mongo_uri = ""
        self.mongo_db = ""
        self.mongo_reachable = True   # test toggles this to simulate a dead server
        self.mongo_migrated = 0       # messages a connect reports as carried over
        self.automations_paused = False

    # apps / status / activity
    def get_running_apps(self):
        return detect_running_apps(self.windows)

    def list_browser_tabs(self, window_handle):
        return list(self.browser_tabs)

    def activate_browser_tab(self, window_handle, tab_title):
        self.activated_tabs.append((window_handle, tab_title))
        return True

    def is_relay_enabled(self):
        return self.relay_enabled

    def get_activity_log(self, limit=200):
        return list(self.activity)[:limit]

    # whatsapp
    def get_whatsapp_chats(self):
        return list(self.chats) if self.chats_available else None

    def can_find_chat(self, chat):
        return chat in self.findable

    def resolve_chat_name(self, chat):
        # A phone number resolves to its saved contact; a known name to itself.
        return getattr(self, "canonical_names", {}).get(chat, chat if chat in self.findable else "")

    def test_message_source(self, url, chat):
        self.tested_sources.append(url)
        return (self.source_ok, "Connected" if self.source_ok else "the website returned an error")

    def is_chat_automation_running(self, chat, reply_source=""):
        return self.relay_enabled and any(
            c == chat and (not reply_source or s == reply_source)
            for c, s in self._running_chats
        )

    def get_chat_bindings(self, chat):
        return [b for b in self.bindings if b.group_name.strip().lower() == chat.strip().lower()]

    # chat memory (view / manage)
    def chat_memory_backend(self):
        return self.chat_memory_backend_name

    def get_chat_memory(self, chat, limit=200):
        return list(self.chat_memory.get(chat, []))

    def get_chats_with_memory(self):
        return [(c, len(m)) for c, m in self.chat_memory.items() if m]

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

    def remember_agent_success(self, app_name, goal, steps_taken):
        self.remembered_successes.append((app_name, goal, tuple(steps_taken)))

    def get_ai_web_search(self):
        return self.ai_web_search

    def set_ai_web_search(self, enabled):
        self.ai_web_search = enabled

    def get_chat_memory_mongo_uri(self):
        return self.mongo_uri

    def set_chat_memory_mongo(self, uri, database=""):
        self.mongo_uri = uri
        self.mongo_db = database
        # A URI "connects" only if the test says the server is reachable.
        connected = bool(uri and self.mongo_reachable)
        self.chat_memory_backend_name = "MongoDB" if connected else "local storage"
        migrated = self.mongo_migrated if connected else 0
        return self.chat_memory_backend_name, migrated

    def get_webhook_testing_enabled(self):
        return self.webhook_testing

    def set_webhook_testing_enabled(self, enabled):
        self.webhook_testing = enabled

    def webhook_pending_count(self):
        return self.webhook_pending

    def get_ai_style(self):
        return self.ai_style

    def set_ai_style(self, style):
        self.ai_style = style

    # saved automations
    def get_automations(self):
        return list(self.automations)

    def get_automations_paused(self):
        return self.automations_paused

    def set_automations_paused(self, paused):
        self.automations_paused = paused

    def save_automation(self, automation_id, name, kind, target, target_display, instruction,
                        trigger_type="manual", schedule_mode="interval", interval_minutes=60,
                        daily_time="09:00", watch_process="", watch_display="", watch_text="",
                        watch_mode="literal"):
        from winspark.ui.engine_host import Automation

        self.saved_automations.append(
            (automation_id, name, kind, target, target_display, instruction)
        )
        self.saved_triggers.append(
            (trigger_type, schedule_mode, interval_minutes, daily_time,
             watch_process, watch_display, watch_text, watch_mode)
        )
        fields = dict(
            name=name, kind=kind, target=target, target_display=target_display,
            instruction=instruction, trigger_type=trigger_type, schedule_mode=schedule_mode,
            interval_minutes=interval_minutes, daily_time=daily_time,
            watch_process=watch_process, watch_display=watch_display, watch_text=watch_text,
            watch_mode=watch_mode,
        )
        if automation_id is None:
            new_id = (max((a.id for a in self.automations), default=0) + 1)
            self.automations.append(Automation(id=new_id, enabled=True, **fields))
            return new_id
        for i, a in enumerate(self.automations):
            if a.id == automation_id:
                from dataclasses import replace

                self.automations[i] = replace(a, **fields)
        return automation_id

    def set_automation_enabled(self, automation_id, enabled):
        from dataclasses import replace

        self.automation_enabled_calls.append((automation_id, enabled))
        for i, a in enumerate(self.automations):
            if a.id == automation_id:
                self.automations[i] = replace(a, enabled=enabled)

    def set_all_automations_enabled(self, enabled):
        from dataclasses import replace

        changed = 0
        for i, a in enumerate(self.automations):
            if a.enabled != enabled:
                self.automations[i] = replace(a, enabled=enabled)
                changed += 1
        return changed

    def delete_automation(self, automation_id):
        self.deleted_automations.append(automation_id)
        self.automations = [a for a in self.automations if a.id != automation_id]

    def run_automation(self, automation_id, progress=None, ask_user=None):
        self.ran_automations.append(automation_id)
        if progress:
            for line in self.run_progress_lines:
                progress(line)
        if self.run_question and ask_user is not None:
            self.run_answer = ask_user(self.run_question)
        return self.run_result

    # openai (app-wide, keys stored per provider)
    def get_openai_api_key(self, provider=""):
        provider = provider or self.ai_provider
        if provider in self.provider_keys:
            return self.provider_keys[provider]
        return self.openai_key if provider == self.ai_provider else ""

    def get_openai_model(self, provider=""):
        provider = provider or self.ai_provider
        return self.provider_models.get(provider, self.openai_model)

    def get_ai_provider(self):
        return self.ai_provider

    def set_openai_config(self, api_key, model="", provider=""):
        provider = provider or self.ai_provider
        self.ai_provider = provider
        self.openai_key = api_key
        self.openai_model = model or "gpt-4o-mini"
        self.provider_keys[provider] = api_key
        self.provider_models[provider] = self.openai_model

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
        self._running_chats.add((chat, reply_source))

    def stop_chat_automation(self, chat, reply_source=""):
        self.stopped.append(chat)
        self._running_chats = {
            (c, s) for c, s in self._running_chats
            if c != chat or (reply_source and s != reply_source)
        }


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
    # The box is pre-filled with the chat's built-in inbox link, so that's
    # what gets tested.
    assert controller.tested_sources == ["http://127.0.0.1:5001/webhook/Family"]


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

    for combo in (whatsapp._method_combo, whatsapp._interval_combo, whatsapp._ai_mode):
        combo.setCurrentIndex(0)
        event = wheel_down(combo)
        assert combo.currentIndex() == 0, f"{combo} changed on wheel"
        assert not event.isAccepted()  # bubbles up so the page still scrolls


def test_start_and_stop_automation_for_the_chosen_chat(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    whatsapp._interval_combo.setCurrentIndex(1)  # every 5 seconds

    whatsapp.toggle_automation()
    # The source box is pre-filled with the built-in inbox link for the chat,
    # so starting the web source uses it (not blank).
    assert controller.started == [("Family", "http://127.0.0.1:5001/webhook/Family", 5)]
    assert whatsapp.is_running() is True
    assert whatsapp._start_button.text() == "Stop automation"

    whatsapp.toggle_automation()
    assert controller.stopped == ["Family"]
    assert whatsapp._start_button.text() == "Start automation"


def test_a_second_automation_type_can_start_on_the_same_chat(whatsapp, controller):
    """One automation per TYPE per chat: with the web link running, switching
    step 2 to AI offers Start (not Stop) — both end up on together, and
    stopping the AI one leaves the web one running."""
    whatsapp._chat_name.setText("Family")

    whatsapp.toggle_automation()                      # start the web automation
    assert whatsapp.is_running() is True

    _select_openai(whatsapp)                          # switch the type in step 2
    assert whatsapp._start_button.text() == "Start automation"   # AI isn't on yet
    whatsapp._ai_prompt.setPlainText("Be brief.")
    whatsapp.toggle_automation()                      # start the AI automation too

    assert controller.is_chat_automation_running("Family", "web")
    assert controller.is_chat_automation_running("Family", "openai")
    assert whatsapp._start_button.text() == "Stop automation"

    whatsapp.toggle_automation()                      # stop ONLY the AI automation
    assert not controller.is_chat_automation_running("Family", "openai")
    assert controller.is_chat_automation_running("Family", "web")   # untouched


def test_source_box_prefills_a_default_inbox_link_tracking_the_chat(whatsapp):
    whatsapp._chat_name.setText("Family")
    assert whatsapp._source.text() == "http://127.0.0.1:5001/webhook/Family"
    whatsapp._chat_name.setText("Work")   # switch chat -> link follows
    assert whatsapp._source.text() == "http://127.0.0.1:5001/webhook/Work"


def test_a_custom_source_url_is_not_overwritten_when_the_chat_changes(whatsapp):
    whatsapp._chat_name.setText("Family")
    whatsapp._source.setText("https://my.own/replies")   # user's own address
    whatsapp._chat_name.setText("Work")
    assert whatsapp._source.text() == "https://my.own/replies"


def test_copy_link_puts_the_inbox_url_on_the_clipboard(whatsapp):
    from PySide6.QtWidgets import QApplication

    whatsapp._chat_name.setText("Family")
    whatsapp._copy_source_link()
    assert QApplication.clipboard().text() == "http://127.0.0.1:5001/webhook/Family"
    assert whatsapp._source_check.state == "ok"


def test_openai_method_shows_mode_and_prompt_only(whatsapp):
    # Default method is the web/test source; the AI panel is hidden.
    assert whatsapp.current_reply_source() == "web"
    assert whatsapp._ai_panel.isVisible() is False

    _select_openai(whatsapp)
    assert whatsapp.current_reply_source() == "openai"
    assert whatsapp._web_panel.isVisible() is False
    # The AI service itself (key/model/provider) lives in Settings now —
    # no key fields inside the WhatsApp panel.
    assert not hasattr(whatsapp, "_ai_key")


def test_test_connection_uses_saved_ai_settings(whatsapp, controller):
    controller.openai_key = "sk-saved"
    _select_openai(whatsapp)
    whatsapp.test_source()
    assert controller.openai_key == "sk-saved"  # untouched: uses saved settings
    assert whatsapp._source_check.state == "ok"


def test_openai_test_connection_shows_plain_failure(whatsapp, controller):
    controller.openai_ok = False
    _select_openai(whatsapp)
    whatsapp.test_source()
    assert whatsapp._source_check.state == "bad"
    assert "HTTP" not in whatsapp._source_check.message


def test_start_with_openai_passes_prompt_and_mode(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    _select_openai(whatsapp)
    whatsapp._ai_prompt.setPlainText("Reply kindly.")
    whatsapp.toggle_automation()

    assert controller.started == [("Family", "", 3)]
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


def test_openai_generate_mode_can_be_selected(whatsapp, controller):
    whatsapp._chat_name.setText("Family")
    _select_openai(whatsapp)
    whatsapp._ai_mode.setCurrentIndex(whatsapp._ai_mode.findData("generate"))
    whatsapp.toggle_automation()

    assert controller.last_start_kwargs["ai_mode"] == "generate"


# --- the Automations panel (create / manage / run) ----------------------------

def _make_automation(id=1, name="Ping", kind=None, target="Family",
                     target_display="Family", instruction="hi", enabled=True):
    from winspark.ui.engine_host import AUTOMATION_WHATSAPP, Automation

    return Automation(id, name, kind or AUTOMATION_WHATSAPP, target, target_display, instruction, enabled)


def _automations_panel(controller):
    from winspark.ui.panels import AutomationsPanel

    panel = AutomationsPanel(controller)
    panel._spawn = lambda worker: worker()  # run "background" work inline
    return panel


def test_turn_all_off_disables_every_automation(qapp, controller):
    controller.automations = [_make_automation(id=1, enabled=True),
                              _make_automation(id=2, name="Two", enabled=True)]
    panel = _automations_panel(controller)
    panel.reload()
    panel.turn_all_off()
    assert all(not a.enabled for a in controller.automations)
    panel.turn_all_on()
    assert all(a.enabled for a in controller.automations)


def test_filter_shows_only_active_or_only_off(qapp, controller):
    controller.automations = [_make_automation(id=1, name="On one", enabled=True),
                              _make_automation(id=2, name="Off one", enabled=False)]
    panel = _automations_panel(controller)

    panel._filter.setCurrentIndex(panel._filter.findData("active"))
    assert panel._rows.count() == 1
    panel._filter.setCurrentIndex(panel._filter.findData("off"))
    assert panel._rows.count() == 1
    panel._filter.setCurrentIndex(panel._filter.findData("all"))
    assert panel._rows.count() == 2


def test_multiselect_bulk_turn_off_selected(qapp, controller):
    controller.automations = [_make_automation(id=1, enabled=True),
                              _make_automation(id=2, name="Two", enabled=True),
                              _make_automation(id=3, name="Three", enabled=True)]
    panel = _automations_panel(controller)
    panel.reload()
    panel._select_btn.setChecked(True)         # enter multi-select
    assert panel._bulk_bar.isHidden() is False
    # Select the rows for automations 1 and 3, then bulk turn-off.
    for aid, check in panel._row_checks:
        if aid in (1, 3):
            check.setChecked(True)
    panel._bulk_set_enabled(False)
    by_id = {a.id: a.enabled for a in controller.automations}
    assert by_id == {1: False, 2: True, 3: False}


def test_multiselect_bulk_delete_asks_and_deletes(qapp, controller):
    controller.automations = [_make_automation(id=1), _make_automation(id=2, name="Two")]
    panel = _automations_panel(controller)
    panel.reload()
    panel._confirm_bulk_delete = lambda count: True   # user confirms
    panel._select_btn.setChecked(True)
    panel._select_all.setChecked(True)                # select all rows
    panel._bulk_delete_selected()
    assert controller.deleted_automations == [1, 2] or controller.deleted_automations == [2, 1]
    assert controller.automations == []


def test_create_whatsapp_automation_persists_and_lists(qapp, controller):
    from winspark.ui.engine_host import AUTOMATION_WHATSAPP

    panel = _automations_panel(controller)
    panel.new_automation()
    panel._type.setCurrentIndex(panel._type.findData(AUTOMATION_WHATSAPP))
    panel._name.setText("Morning ping")
    panel._wa_chat.setText("Family")
    panel._wa_message.setPlainText("Good morning!")
    panel.save()

    assert controller.saved_automations == [
        (None, "Morning ping", AUTOMATION_WHATSAPP, "Family", "Family", "Good morning!")
    ]
    assert panel._rows.count() == 1  # now listed
    assert panel._editor.isHidden()


def test_create_app_action_automation_uses_selected_app(qapp, controller):
    from winspark.ui.engine_host import AUTOMATION_APP_ACTION

    panel = _automations_panel(controller)
    panel.new_automation()
    panel._type.setCurrentIndex(panel._type.findData(AUTOMATION_APP_ACTION))
    panel._name.setText("Search flights")
    i = panel._app_combo.findData("notepad.exe")
    panel._app_combo.setCurrentIndex(i)
    panel._app_instruction.setPlainText("type my shopping list")
    panel.save()

    saved = controller.saved_automations[-1]
    assert saved[2] == AUTOMATION_APP_ACTION
    assert saved[3] == "notepad.exe"          # target = process name
    assert saved[5] == "type my shopping list"


def test_save_validates_required_fields(qapp, controller):
    panel = _automations_panel(controller)
    panel.new_automation()
    panel._name.setText("")  # no name
    panel.save()
    assert controller.saved_automations == []
    assert panel._editor_check.state == "bad"

    panel._name.setText("Named but empty")
    panel._wa_chat.setText("")
    panel.save()
    assert controller.saved_automations == []  # still blocked (missing chat/message)


def test_edit_automation_updates_in_place(qapp, controller):
    from winspark.ui.engine_host import AUTOMATION_WHATSAPP

    controller.automations = [_make_automation(id=7, name="Old", instruction="old text")]
    panel = _automations_panel(controller)
    panel.edit_automation(controller.automations[0])
    panel._wa_message.setPlainText("new text")
    panel.save()

    assert controller.saved_automations[-1][0] == 7  # updates, not inserts
    assert controller.saved_automations[-1][5] == "new text"


def test_toggle_and_delete_automation(qapp, controller):
    controller.automations = [_make_automation(id=3, enabled=True)]
    panel = _automations_panel(controller)

    panel.toggle_enabled(controller.automations[0])
    assert controller.automation_enabled_calls == [(3, False)]

    panel.delete_automation(_make_automation(id=3))
    assert controller.deleted_automations == [3]
    assert panel._rows.count() == 0


def test_run_automation_reports_result(qapp, controller):
    controller.automations = [_make_automation(id=5, name="Ping")]
    controller.run_result = (True, "Message sent.")
    panel = _automations_panel(controller)

    panel.run_automation(controller.automations[0])
    assert controller.ran_automations == [5]
    assert panel._run_check.state == "ok"
    assert "sent" in panel._run_check.message.lower()


def test_run_automation_shows_failure(qapp, controller):
    controller.automations = [_make_automation(id=6)]
    controller.run_result = (False, "Chrome isn’t open right now.")
    panel = _automations_panel(controller)

    panel.run_automation(controller.automations[0])
    assert panel._run_check.state == "bad"


def test_create_scheduled_automation_saves_trigger(qapp, controller):
    from winspark.ui.engine_host import TRIGGER_SCHEDULE

    panel = _automations_panel(controller)
    panel.new_automation()
    panel._name.setText("Daily standup")
    panel._wa_chat.setText("Team")
    panel._wa_message.setPlainText("Standup time!")
    panel._trigger.setCurrentIndex(panel._trigger.findData(TRIGGER_SCHEDULE))
    panel._sched_mode.setCurrentIndex(panel._sched_mode.findData("daily"))
    from PySide6.QtCore import QTime

    panel._daily_time.setTime(QTime(9, 30))
    panel.save()

    trig = controller.saved_triggers[-1]
    assert trig[0] == TRIGGER_SCHEDULE and trig[1] == "daily" and trig[3] == "09:30"


def test_create_screen_trigger_requires_app_and_text(qapp, controller):
    from winspark.ui.engine_host import TRIGGER_SCREEN

    panel = _automations_panel(controller)
    panel.new_automation()
    panel._name.setText("Alert me on error")
    panel._wa_chat.setText("Me")
    panel._wa_message.setPlainText("Something errored!")
    panel._trigger.setCurrentIndex(panel._trigger.findData(TRIGGER_SCREEN))
    panel._watch_text.setText("")  # missing watch text
    panel.save()
    assert controller.saved_automations == []
    assert panel._editor_check.state == "bad"

    panel._watch_app.setCurrentIndex(panel._watch_app.findData("notepad.exe"))
    panel._watch_text.setText("error")
    panel.save()
    trig = controller.saved_triggers[-1]
    assert trig[0] == TRIGGER_SCREEN and trig[4] == "notepad.exe" and trig[6] == "error"


def test_trigger_fields_show_only_for_their_trigger(qapp, controller):
    from winspark.ui.engine_host import TRIGGER_MANUAL, TRIGGER_SCHEDULE, TRIGGER_SCREEN

    panel = _automations_panel(controller)
    panel.new_automation()
    panel._trigger.setCurrentIndex(panel._trigger.findData(TRIGGER_MANUAL))
    assert panel._sched_panel.isHidden() and panel._screen_panel.isHidden()
    panel._trigger.setCurrentIndex(panel._trigger.findData(TRIGGER_SCHEDULE))
    assert not panel._sched_panel.isHidden() and panel._screen_panel.isHidden()
    panel._trigger.setCurrentIndex(panel._trigger.findData(TRIGGER_SCREEN))
    assert panel._sched_panel.isHidden() and not panel._screen_panel.isHidden()


def test_edit_prefills_schedule(qapp, controller):
    from winspark.ui.engine_host import TRIGGER_SCHEDULE

    controller.automations = [_make_automation(
        id=4, name="Hourly", enabled=True)]
    from dataclasses import replace

    controller.automations[0] = replace(
        controller.automations[0], trigger_type=TRIGGER_SCHEDULE,
        schedule_mode="interval", interval_minutes=30)
    panel = _automations_panel(controller)
    panel.edit_automation(controller.automations[0])
    assert panel._trigger.currentData() == TRIGGER_SCHEDULE
    assert panel._interval.currentData() == 30


def test_duplicate_creates_a_manual_copy(qapp, controller):
    from winspark.ui.engine_host import TRIGGER_SCHEDULE

    controller.automations = [_make_automation(id=8, name="Nightly")]
    panel = _automations_panel(controller)
    panel.duplicate_automation(controller.automations[0])

    last = controller.saved_automations[-1]
    assert last[0] is None and last[1] == "Nightly (1)"  # numbered, not "(copy)"
    # a duplicated schedule must NOT start firing on its own
    assert controller.saved_triggers[-1][0] != TRIGGER_SCHEDULE


def test_delete_asks_for_confirmation(qapp, controller):
    controller.automations = [_make_automation(id=2)]
    panel = _automations_panel(controller)

    panel._confirm_delete = lambda automation: False  # user clicks "No"
    panel.confirm_delete(controller.automations[0])
    assert controller.deleted_automations == []

    panel._confirm_delete = lambda automation: True   # user clicks "Yes"
    panel.confirm_delete(controller.automations[0])
    assert controller.deleted_automations == [2]


def test_run_streams_live_progress(qapp, controller):
    controller.automations = [_make_automation(id=1)]
    controller.run_progress_lines = ["→ Click “Save”", "   ✓ Click “Save”"]
    panel = _automations_panel(controller)

    panel.run_automation(controller.automations[0])
    log = panel._run_log.toPlainText()
    assert "Click “Save”" in log
    assert not panel._run_log.isHidden()


def test_pause_all_switch_reflects_and_updates_controller(qapp, controller):
    controller.automations_paused = True
    panel = _automations_panel(controller)
    assert panel._pause_all.isChecked() is True
    assert not panel._pause_banner.isHidden()  # banner shows while paused

    panel._pause_all.setChecked(False)
    assert controller.automations_paused is False
    assert panel._pause_banner.isHidden()


def test_screen_trigger_saves_match_mode(qapp, controller):
    from winspark.ui.engine_host import TRIGGER_SCREEN

    panel = _automations_panel(controller)
    panel.new_automation()
    panel._name.setText("Alert by meaning")
    panel._wa_chat.setText("Me")
    panel._wa_message.setPlainText("Heads up!")
    panel._trigger.setCurrentIndex(panel._trigger.findData(TRIGGER_SCREEN))
    panel._watch_app.setCurrentIndex(panel._watch_app.findData("notepad.exe"))
    panel._watch_text.setText("the build failed")
    panel._watch_mode.setCurrentIndex(panel._watch_mode.findData("meaning"))
    panel.save()

    assert controller.saved_triggers[-1][7] == "meaning"


def test_edit_prefills_screen_match_mode(qapp, controller):
    from dataclasses import replace

    from winspark.ui.engine_host import TRIGGER_SCREEN

    controller.automations = [_make_automation(id=5)]
    controller.automations[0] = replace(
        controller.automations[0], trigger_type=TRIGGER_SCREEN,
        watch_process="chrome.exe", watch_display="Chrome",
        watch_text="done", watch_mode="meaning")
    panel = _automations_panel(controller)
    panel.edit_automation(controller.automations[0])
    assert panel._watch_mode.currentData() == "meaning"


def test_automations_open_from_the_rail(window):
    window._open_automations()
    assert window._stack.currentWidget() is window._automations_panel


def test_automations_view_survives_a_sidebar_rebuild(window):
    # Reproduces the reported bug: running an automation can open/close an app,
    # which rebuilds the sidebar and fires a spurious deselect — the Automations
    # view must NOT flip back to Welcome/an app.
    window._open_automations()
    assert window._stack.currentWidget() is window._automations_panel

    # spurious deselect (what a rebuild triggers)
    window._on_app_selected(None, None)
    assert window._stack.currentWidget() is window._automations_panel

    # even a full refresh after the app list changed keeps us on Automations
    window._controller.windows = window._controller.windows + [
        WindowInfo(handle=99, title="Calc", process_name="calc.exe")
    ]
    window.refresh()
    assert window._stack.currentWidget() is window._automations_panel


def test_selecting_a_real_app_leaves_the_rail_view(window):
    window._open_automations()
    # Simulate a real click on an app row (sets the row, then the click signal).
    window._sidebar.setCurrentRow(0)
    window._sidebar.itemClicked.emit(window._sidebar.item(0))
    assert window._stack.currentWidget() is not window._automations_panel
    assert window._active_rail is None


def test_programmatic_selection_change_cannot_leave_a_rail_view(window):
    # A rebuild's currentItemChanged (no user click) must NOT navigate away.
    window._open_automations()
    window._sidebar.setCurrentRow(0)  # programmatic — like a rebuild auto-select
    assert window._stack.currentWidget() is window._automations_panel
    assert window._active_rail == "automations"


# --- the Settings panel (app-wide AI service) ---------------------------------

def test_settings_mongo_save_connects_and_reports_backend(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    controller.mongo_reachable = True
    panel = SettingsPanel(controller)
    panel._mongo_uri.setText("mongodb://localhost:27017")
    panel._mongo_db.setText("winspark")
    panel.save_chat_memory()

    assert controller.mongo_uri == "mongodb://localhost:27017"
    assert controller.mongo_db == "winspark"
    assert panel._mongo_check.state == "ok"
    assert "MongoDB" in panel._mongo_check.message


def test_settings_mongo_connect_reports_migrated_message_count(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    controller.mongo_reachable = True
    controller.mongo_migrated = 7   # 7 remembered messages carried over from local
    panel = SettingsPanel(controller)
    panel._mongo_uri.setText("mongodb://localhost:27017")
    panel.save_chat_memory()

    assert panel._mongo_check.state == "ok"
    assert "Moved 7 remembered messages over" in panel._mongo_check.message


def test_settings_mongo_save_reports_fallback_when_unreachable(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    controller.mongo_reachable = False   # server refuses / doesn't exist
    panel = SettingsPanel(controller)
    panel._mongo_uri.setText("mongodb://dead:27017")
    panel.save_chat_memory()

    assert panel._mongo_check.state == "bad"
    assert "local storage" in panel._mongo_check.message


def test_settings_mongo_blank_uri_means_local(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    panel = SettingsPanel(controller)
    panel._mongo_uri.setText("")
    panel.save_chat_memory()

    assert controller.mongo_uri == ""
    assert panel._mongo_check.state == "ok"
    assert "local storage" in panel._mongo_check.message.lower()


def test_settings_panel_prefills_saved_values(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    controller.openai_key = "gsk-saved"
    controller.ai_provider = "groq"
    controller.openai_model = "llama-3.3-70b-versatile"
    panel = SettingsPanel(controller)

    assert panel._key.text() == "gsk-saved"
    assert panel._provider.currentData() == "groq"
    assert panel._model.text() == "llama-3.3-70b-versatile"


def test_settings_panel_save_persists_provider_key_model(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    panel = SettingsPanel(controller)
    panel._provider.setCurrentIndex(panel._provider.findData("groq"))
    panel._key.setText("gsk-new")
    panel._model.setText("llama-3.3-70b-versatile")
    panel.save()

    assert controller.ai_provider == "groq"
    assert controller.openai_key == "gsk-new"
    assert controller.openai_model == "llama-3.3-70b-versatile"
    assert panel._check.state == "ok"


def test_switching_provider_shows_that_providers_key_not_the_other(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    # OpenAI has a saved key; Groq has none.
    controller.ai_provider = "openai"
    controller.provider_keys = {"openai": "sk-openai-key"}
    panel = SettingsPanel(controller)
    assert panel._key.text() == "sk-openai-key"

    # Switching to Groq must clear the box (Groq has no key yet), not keep sk-…
    panel._provider.setCurrentIndex(panel._provider.findData("groq"))
    assert panel._key.text() == ""

    # Back to OpenAI restores its key.
    panel._provider.setCurrentIndex(panel._provider.findData("openai"))
    assert panel._key.text() == "sk-openai-key"


def test_web_search_toggle_reflects_and_updates_controller(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    controller.ai_web_search = False
    panel = SettingsPanel(controller)
    assert panel._web_search.isChecked() is False

    panel._web_search.setChecked(True)
    assert controller.ai_web_search is True


def test_settings_panel_test_connection_saves_then_tests(qapp, controller):
    from winspark.ui.panels import SettingsPanel

    panel = SettingsPanel(controller)
    panel._key.setText("sk-try")
    panel.test_connection()
    assert controller.openai_key == "sk-try"
    assert panel._check.state == "ok"

    controller.openai_ok = False
    panel.test_connection()
    assert panel._check.state == "bad"


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


def test_sending_returns_focus_to_winspark(whatsapp, controller):
    """Sending foregrounds WhatsApp; winSpark should come back afterwards."""
    calls = []
    whatsapp._return_to_front = lambda: calls.append(True)
    whatsapp._chat_name.setText("Family")
    whatsapp._compose.setText("hi")
    whatsapp.send_message()
    assert calls == [True]


def test_opening_a_chat_returns_focus_to_winspark(whatsapp, controller):
    calls = []
    whatsapp._return_to_front = lambda: calls.append(True)
    whatsapp._chat_name.setText("Work")
    whatsapp.open_chat()
    assert calls == [True]


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
    assert "doesn't have a dedicated integration yet" in panel._body.text()


def test_open_tabs_are_selectable_for_a_browser_window(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    controller.windows = [WindowInfo(handle=5, title="Gmail", process_name="chrome.exe")]
    controller.browser_tabs = [("Gmail", False), ("YouTube", True), ("Docs", False)]
    chrome = next(a for a in detect_running_apps(controller.windows) if a.display_name == "Chrome")
    panel = GenericAppPanel(controller)
    panel._spawn = lambda w: w()  # inline
    panel.set_app(chrome)

    assert not panel._tab_row.isHidden()
    assert [panel._tab_combo.itemText(i) for i in range(panel._tab_combo.count())] == ["Gmail", "YouTube", "Docs"]
    assert panel._tab_combo.currentData() == "YouTube"  # the current tab is pre-selected


def test_picking_a_tab_switches_the_browser_to_it(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    controller.windows = [WindowInfo(handle=5, title="Gmail", process_name="chrome.exe")]
    controller.browser_tabs = [("Gmail", True), ("YouTube", False)]
    chrome = next(a for a in detect_running_apps(controller.windows) if a.display_name == "Chrome")
    panel = GenericAppPanel(controller)
    panel._spawn = lambda w: w()
    panel.set_app(chrome)

    panel._tab_combo.setCurrentIndex(panel._tab_combo.findData("YouTube"))
    panel._on_tab_selected()
    assert controller.activated_tabs == [(5, "YouTube")]


def test_mid_run_guidance_is_queued_and_drained_once(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    panel = GenericAppPanel(controller)
    panel._agent_busy = True   # pretend a run is in progress

    panel._guide_input.setText("try the search box instead")
    panel._send_guidance()

    # queued for the loop, and echoed to the log exactly once
    assert panel._pending_guidance == ["try the search box instead"]
    assert panel._agent_view.toPlainText().count("try the search box instead") == 1

    # the loop drains it (and does not re-echo — see the drain site)
    assert panel._drain_guidance() == ["try the search box instead"]
    assert panel._pending_guidance == []


def test_guidance_answers_a_pending_question_instead_of_queuing(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    panel = GenericAppPanel(controller)
    panel._agent_busy = True
    panel._on_agent_question("Which account?")   # agent is blocked on a question
    assert panel._question_pending

    panel._guide_input.setText("the work one")
    panel._send_guidance()

    # routed to the answer handshake, not the guidance queue
    assert panel._pending_guidance == []
    assert panel._question_answer == "the work one"
    assert not panel._question_pending


def test_tab_selector_hidden_when_only_one_tab(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    controller.windows = [WindowInfo(handle=5, title="Gmail", process_name="chrome.exe")]
    controller.browser_tabs = [("Gmail", True)]
    chrome = next(a for a in detect_running_apps(controller.windows) if a.display_name == "Chrome")
    panel = GenericAppPanel(controller)
    panel._spawn = lambda w: w()
    panel.set_app(chrome)
    assert panel._tab_row.isHidden()  # nothing to choose between


def test_tab_read_is_throttled_to_title_changes(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    controller.windows = [WindowInfo(handle=5, title="Gmail", process_name="chrome.exe")]
    controller.browser_tabs = [("Gmail", True), ("YouTube", False)]
    reads = []
    controller.list_browser_tabs = lambda h: reads.append(h) or [("Gmail", True), ("YouTube", False)]
    chrome = next(a for a in detect_running_apps(controller.windows) if a.display_name == "Chrome")
    panel = GenericAppPanel(controller)
    panel._spawn = lambda w: w()
    panel.set_app(chrome)
    panel.refresh_tabs()  # same title -> no new read
    panel.refresh_tabs()
    assert len(reads) == 1  # only the initial read


def test_tab_read_reruns_when_window_title_changes(qapp, controller):
    # Regression: a single-window browser used to NEVER re-read its tabs, because
    # a duplicate _selected_window_title returned "" for apps with <2 windows, so
    # the throttle key never changed. Switching a tab changes the window title and
    # MUST trigger a fresh read.
    from winspark.ui.panels import GenericAppPanel

    controller.windows = [WindowInfo(handle=5, title="Gmail", process_name="chrome.exe")]
    reads = []
    controller.list_browser_tabs = lambda h: reads.append(h) or [("Gmail", True), ("YouTube", False)]
    chrome = next(a for a in detect_running_apps(controller.windows) if a.display_name == "Chrome")
    panel = GenericAppPanel(controller)
    panel._spawn = lambda w: w()
    panel.set_app(chrome)                 # initial forced read
    assert len(reads) == 1
    panel.refresh_tabs()                  # same title -> throttled, no re-read
    assert len(reads) == 1

    # The user switches tab: the single browser window's title changes.
    switched = detect_running_apps([WindowInfo(handle=5, title="YouTube", process_name="chrome.exe")])
    panel._app = next(a for a in switched if a.display_name == "Chrome")
    panel.refresh_tabs()                  # changed title -> must re-read
    assert len(reads) == 2


def test_guidance_typed_mid_run_reaches_the_agent(qapp, controller):
    controller.agent_script = [
        _decision(step=_step("click", "Save")),
        _decision(done=True, summary="Saved."),
    ]
    panel = _generic_panel_with_notepad(controller)

    # Simulate the user typing guidance DURING the run: on the first agent turn,
    # queue a note. The next round's drain must fold it into the history.
    original = controller.agent_next_step
    def next_step_and_guide(handle, app, goal, history):
        if len(controller.agent_next_calls) == 0:
            panel._guide_input.setText("use the toolbar button")
            panel._send_guidance()
        return original(handle, app, goal, history)
    controller.agent_next_step = next_step_and_guide

    panel._agent_input.setText("save it")
    panel.do_it()

    # The note reached the agent on a later turn.
    all_history = [h for call in controller.agent_next_calls for h in call[3]]
    assert any("use the toolbar button" in h for h in all_history)


def test_ask_and_act_share_one_section_toggled_by_mode(qapp, controller):
    from winspark.ui.panels import GenericAppPanel

    apps = detect_running_apps(controller.windows)
    notepad = next(a for a in apps if a.display_name == "Notepad")
    panel = GenericAppPanel(controller)
    panel.set_app(notepad)

    # Ask is the default; the acting controls are hidden.
    assert panel._interact_mode.currentData() == "ask"
    assert panel._act_panel.isHidden() and not panel._ask_panel.isHidden()

    panel._interact_mode.setCurrentIndex(panel._interact_mode.findData("act"))
    assert not panel._act_panel.isHidden() and panel._ask_panel.isHidden()


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


def test_stop_button_interrupts_the_loop(qapp, controller):
    controller.agent_script = [
        _decision(step=_step("click", f"Next {i}"), digest=f"d{i}") for i in range(5)
    ]
    panel = _generic_panel_with_notepad(controller)

    # Press Stop while the first step is executing (inline spawn runs the whole
    # loop synchronously, so the side effect stands in for a mid-run click).
    original = controller.agent_execute_step
    def execute_and_stop(handle, step):
        result = original(handle, step)
        panel.stop_agent()
        return result
    controller.agent_execute_step = execute_and_stop

    panel._agent_input.setText("click next forever")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 1  # stopped after the in-flight step
    assert "Stopped by you" in panel._agent_check.message
    assert panel._stop_btn.isEnabled() is False  # re-disabled once idle


def test_long_runs_check_in_with_the_user_instead_of_capping(qapp, controller):
    # No fixed budget any more: at the 15-round check-in the user decides.
    controller.agent_script = [
        _decision(step=_step("click", f"Next {i}"), digest=f"d{i}") for i in range(40)
    ]
    panel = _generic_panel_with_notepad(controller)
    asked = []
    panel._request_approval = lambda text: (asked.append(text), False)[1]  # user says stop
    panel._agent_input.setText("click next forever")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 14  # rounds 1-14, then the check-in
    assert asked and "keep going" in asked[0]
    assert "14 steps" in panel._agent_check.message


def test_check_in_approved_keeps_the_loop_going(qapp, controller):
    controller.agent_script = (
        [_decision(step=_step("click", f"Next {i}"), digest=f"d{i}") for i in range(16)]
        + [_decision(done=True, summary="All done.")]
    )
    panel = _generic_panel_with_notepad(controller)
    panel._request_approval = lambda text: True  # keep going
    panel._agent_input.setText("click through")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 16
    assert panel._agent_check.state == "ok"


def test_agent_question_pauses_for_the_users_answer(qapp, controller):
    from winspark.automation.screen_agent import StepDecision

    controller.agent_script = [
        StepDecision(done=False, question="Which account should I use?"),
        _decision(step=_step("click", "Personal"), digest="d2"),
        _decision(done=True, summary="Signed in."),
    ]
    panel = _generic_panel_with_notepad(controller)
    panel._ask_user = lambda q: "the personal one"
    panel._agent_input.setText("sign in")
    panel.do_it()

    # The answer went into the history the AI sees next round.
    assert any("you said: the personal one" in h for h in controller.agent_next_calls[1][3])
    assert panel._agent_check.state == "ok"


def test_agent_question_with_no_answer_stops(qapp, controller):
    from winspark.automation.screen_agent import StepDecision

    controller.agent_script = [StepDecision(done=False, question="Proceed with which file?")]
    panel = _generic_panel_with_notepad(controller)
    panel._ask_user = lambda q: None  # user stopped / never answered
    panel._agent_input.setText("open it")
    panel.do_it()

    assert controller.agent_steps_executed == []
    assert panel._agent_check.state == "bad"


def test_failed_step_falls_back_instead_of_aborting(qapp, controller):
    # First step fails; the AI sees the failure and succeeds differently.
    controller.agent_script = [
        _decision(step=_step("click", "Save"), digest="d1"),
        _decision(step=_step("press", "Menu"), digest="d2"),
        _decision(done=True, summary="Saved via the menu."),
    ]
    results = [(False, "Couldn't click Save"), (True, "Press CTRL+S")]
    controller.agent_execute_step = lambda h, s, _r=results: (
        controller.agent_steps_executed.append(s) or _r.pop(0)
    )
    panel = _generic_panel_with_notepad(controller)
    panel._agent_input.setText("save the file")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 2      # kept going after the miss
    assert panel._agent_check.state == "ok"
    assert any("FAILED" in h for h in controller.agent_next_calls[1][3])


def test_three_straight_failures_ask_the_user(qapp, controller):
    controller.agent_script = [
        _decision(step=_step("click", f"Try {i}"), digest=f"d{i}") for i in range(3)
    ]
    controller.agent_execute_result = (False, "no luck")
    panel = _generic_panel_with_notepad(controller)
    asked = []
    panel._ask_user = lambda q: (asked.append(q), None)[1]  # no answer -> stop
    panel._agent_input.setText("keep trying")
    panel.do_it()

    assert len(controller.agent_steps_executed) == 3
    assert asked and "kept failing" in asked[0]
    assert panel._agent_check.state == "bad"


def test_finished_run_is_remembered_for_next_time(qapp, controller):
    panel = _generic_panel_with_notepad(controller)
    panel._agent_input.setText("save the file")
    panel.do_it()

    assert controller.remembered_successes
    app_name, goal, steps = controller.remembered_successes[0]
    assert goal == "save the file" and "Notepad" in app_name


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
    assert panel._table.rowCount() == 2
    # Column 1 is the colored Passed/Failed badge; column 2 the plain-English line.
    assert panel._table.item(0, 1).text() == "Passed"       # outcome "ok"
    assert "Sent the message" in panel._table.item(0, 2).text()
    assert panel._table.item(1, 2).text().startswith("Automation started")


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


def test_sidebar_starts_closed_and_pins_open_from_the_rail(window):
    # Default: drawer closed (hover-to-peek), reachable via the ☰ rail button.
    assert window._left.isHidden() is True
    assert window._sidebar_pinned is False

    window._apps_btn.setChecked(True)
    window._toggle_sidebar_pin()          # click ☰: pin open
    assert window._sidebar_pinned is True
    assert window._left.isHidden() is False

    window._apps_btn.setChecked(False)
    window._toggle_sidebar_pin()          # click again: unpin -> closed
    assert window._sidebar_pinned is False
    assert window._left.isHidden() is True


def test_sidebar_peeks_open_on_hover_then_collapses(window):
    # Hovering the ☰ button reveals the drawer as an overlay without pinning.
    window._peek_sidebar()
    assert window._left.isHidden() is False
    assert window._sidebar_pinned is False
    # With the cursor away (nothing under mouse in the offscreen test), the
    # debounced collapse hides it again.
    window._collapse_if_away()
    assert window._left.isHidden() is True


def test_settings_opens_from_the_sidebar_and_app_selection_returns(window):
    window._open_settings()
    assert window._stack.currentWidget() is window._settings_panel

    window._sidebar.setCurrentRow(0)  # click WhatsApp again
    window._sidebar.itemClicked.emit(window._sidebar.item(0))
    assert window._stack.currentWidget() is window._whatsapp_panel


def test_activity_panel_starts_closed_and_toggles_from_the_rail(window):
    # Default closed; the ▤ rail button shows/hides it.
    assert window._activity_panel.isHidden() is True
    window._activity_btn.setChecked(True)
    assert window._activity_panel.isHidden() is False
    window._activity_btn.setChecked(False)
    assert window._activity_panel.isHidden() is True


def test_run_automation_asks_inline_when_the_agent_needs_input(qapp, controller):
    """A manual Run now is attended: the agent's mid-run question goes to the
    panel's answer box instead of aborting with "run it from the Do-it box"."""
    controller.automations = [_make_automation(id=7, name="goa")]
    controller.run_question = "What is today's date?"
    controller.run_result = (True, "Booked.")
    panel = _automations_panel(controller)
    panel._ask_user = lambda q: "12 July 2026"   # sidestep the blocking handshake

    panel.run_automation(controller.automations[0])

    assert controller.run_answer == "12 July 2026"   # the answer reached the run
    assert panel._run_check.state == "ok"


def test_automations_question_box_shows_and_submits(qapp, controller):
    panel = _automations_panel(controller)

    panel._on_run_question("Which departure date?")
    assert panel._question_label.text() == "Which departure date?"

    panel._answer_input.setText("tomorrow")
    panel._submit_answer()

    assert panel._question_answer == "tomorrow"
    assert panel._question_event.is_set()            # the worker is unblocked


def test_agent_reasking_an_answered_question_reuses_the_answer(qapp, controller):
    """Seen live: the agent asked the same question again right after being
    answered. The loop replays the stored answer (prompting the user only
    once); a stubborn third ask stops the run instead of nagging forever."""
    from winspark.automation.screen_agent import StepDecision

    controller.agent_script = [
        StepDecision(done=False, question="What is today's date?"),
        StepDecision(done=False, question="What is today's date?"),   # re-asked
        _decision(done=True, summary="Booked."),
    ]
    asked = []
    panel = _generic_panel_with_notepad(controller)
    panel._ask_user = lambda q: (asked.append(q), "12 July 2026")[1]
    panel._agent_input.setText("book for tomorrow")
    panel.do_it()

    assert asked == ["What is today's date?"]        # prompted exactly once
    assert panel._agent_check.state == "ok"          # the run still finished
    # The replayed answer went back into the history the AI sees.
    assert any("do NOT ask this again" in h for h in controller.agent_next_calls[2][3])


def test_agent_stuck_reasking_forever_stops_the_run(qapp, controller):
    from winspark.automation.screen_agent import StepDecision

    controller.agent_script = [
        StepDecision(done=False, question="Which file?"),
        StepDecision(done=False, question="Which file?"),
        StepDecision(done=False, question="Which file?"),   # third time — give up
        _decision(done=True, summary="never reached"),
    ]
    asked = []
    panel = _generic_panel_with_notepad(controller)
    panel._ask_user = lambda q: (asked.append(q), "the report")[1]
    panel._agent_input.setText("open it")
    panel.do_it()

    assert asked == ["Which file?"]                  # still only prompted once
    assert panel._agent_check.state == "bad"
    assert "kept re-asking" in panel._agent_check.message


def test_memory_view_shows_the_selected_chats_memory(qapp, whatsapp, controller):
    controller.chat_memory = {"Family": [
        ("them", "Dan", "hi, I'm Dan"),
        ("me", "", "hey Dan!"),
    ]}
    whatsapp._chat_name.setText("Family")
    whatsapp._refresh_memory_view()

    text = whatsapp._memory_view.toPlainText()
    assert "them (Dan): hi, I'm Dan" in text
    assert "winSpark: hey Dan!" in text
    assert "2 messages remembered" in whatsapp._memory_backend_label.text()
    assert "local storage" in whatsapp._memory_backend_label.text()


def test_memory_view_empty_for_a_chat_with_no_memory(qapp, whatsapp, controller):
    controller.chat_memory = {}
    whatsapp._chat_name.setText("Work")
    whatsapp._refresh_memory_view()

    assert whatsapp._memory_view.toPlainText() == ""
    assert "Nothing remembered" in whatsapp._memory_backend_label.text()


def test_memory_view_has_no_clear_button(qapp, whatsapp):
    """Memory is persistent — there is deliberately no way to clear it here."""
    assert not hasattr(whatsapp, "_memory_clear_btn")
    assert not hasattr(whatsapp, "_clear_memory")


def test_memory_view_falls_back_to_the_open_chat_name(qapp, whatsapp, controller):
    """Memory is stored under WhatsApp's conversation title (e.g. a phone
    number) which can differ from the selected name. When the selected chat
    has no memory, the viewer shows the open chat's memory instead of "empty"."""
    # Selected "Karthik" has nothing; the open chat is a phone number that does.
    controller.chat_memory = {"+91 89784 67411": [("them", "Karthik", "you free?")]}
    whatsapp._chat_name.setText("Karthik")
    whatsapp._active_chat_name = "+91 89784 67411"

    whatsapp._refresh_memory_view()

    assert "them (Karthik): you free?" in whatsapp._memory_view.toPlainText()
    assert "+91 89784 67411" in whatsapp._memory_backend_label.text()


def test_memory_view_prefers_the_selected_chat_when_it_has_memory(qapp, whatsapp, controller):
    controller.chat_memory = {
        "Manohar": [("them", "", "picked one")],
        "+91 000": [("them", "", "open one")],
    }
    whatsapp._chat_name.setText("Manohar")
    whatsapp._active_chat_name = "+91 000"

    whatsapp._refresh_memory_view()

    assert "picked one" in whatsapp._memory_view.toPlainText()
    assert "open one" not in whatsapp._memory_view.toPlainText()


def test_open_chat_short_circuits_when_already_showing(whatsapp, controller):
    # If winSpark is already showing the chat, "Open chat" must NOT drive
    # WhatsApp again (which can fail to re-open an emoji-named chat and wrongly
    # report "Couldn't open this chat").
    whatsapp._chat_name.setText("Papa 💜")
    whatsapp._active_chat_name = "Papa 💜"   # what refresh_messages sets from the live read
    whatsapp.open_chat()
    assert controller.opened_chats == []      # never called the controller
    assert "Couldn't open" not in whatsapp._send_check.text()


def test_open_chat_drives_whatsapp_when_a_different_chat_is_showing(whatsapp, controller):
    whatsapp._chat_name.setText("Papa 💜")
    whatsapp._active_chat_name = "Someone Else"   # a different chat is open
    whatsapp._spawn = lambda w: w()               # run the worker inline
    whatsapp.open_chat()
    assert controller.opened_chats == ["Papa 💜"]  # it did drive WhatsApp


def test_check_chat_canonicalizes_a_number_to_the_contact_name(whatsapp, controller):
    # Typing a phone number that belongs to a saved contact updates the field to
    # the contact name — so sends/opens use the recents row and memory unifies.
    controller.findable = {"Papa 💜"}
    controller.canonical_names = {"+91 79811 49423": "Papa 💜"}
    whatsapp._chat_name.setText("+91 79811 49423")
    whatsapp.check_chat()
    assert whatsapp.current_chat() == "Papa 💜"
    assert "Found this chat" in whatsapp._chat_check.text()
