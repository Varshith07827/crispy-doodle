"""The Message Hub panel: Refresh list → pick a chat → link a webhook and/or
save that chat's messages.

The two flows must stay independent in the UI as well as underneath — switching
one on must never switch the other on.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from winspark.connectors.models import WhatsAppChatRow  # noqa: E402
from winspark.hub.settings_files import DEFAULT_COLLECTION, HubSettings  # noqa: E402


class FakeHubController:
    """The slice of EngineHost the panel actually uses."""

    def __init__(self, tmp_path):
        self.settings = HubSettings(tmp_path)
        self.chats = [
            WhatsAppChatRow(chat_name="Varshith", timestamp_text="", last_message="",
                            unread_count=0, raw_text="Varshith"),
            WhatsAppChatRow(chat_name="Nagen US", timestamp_text="", last_message="",
                            unread_count=0, raw_text="Nagen US"),
        ]
        self.chats_available = True
        self.mongo_ok = True
        self.capture_line = "Saved 0 messages"

    def get_whatsapp_chats(self):
        return self.chats if self.chats_available else None

    def hub_config(self):
        return self.settings.config

    def hub_save_mongo(self, uri, collection):
        self.settings.update_config(mongo_uri=uri, mongo_collection=collection)
        if not self.mongo_ok:
            return False, "No MongoDB server answered."
        return True, f"Connected — saving to db.{collection}"

    def hub_send_link(self, chat):
        return self.settings.send_link_for(chat)

    def hub_set_send_link(self, chat, url, enabled, interval):
        self.settings.set_send_link(chat, url, enabled, interval)

    def hub_remove_send_link(self, chat):
        self.settings.remove_send_link(chat)

    def hub_is_capturing(self, chat):
        return self.settings.is_capturing(chat)

    def hub_set_capture(self, chat, enabled):
        self.settings.set_capture(chat, enabled)

    def hub_capture_status(self):
        return self.capture_line

    def hub_spool_status(self, chat):
        link = self.settings.send_link_for(chat)
        if link is None:
            return "Not linked to a webhook."
        return "Watching every 3s — 0 sent." if link.enabled else "Paused."


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(tmp_path):
    return FakeHubController(tmp_path)


@pytest.fixture
def panel(qapp, controller):
    from winspark.ui.hub_panel import HubPanel

    p = HubPanel(controller)
    p._spawn = lambda worker: worker()      # run background work inline
    return p


def _pick(panel, chat):
    panel.refresh_chats()
    panel._chat_combo.setCurrentText(chat)


# --- refresh list -------------------------------------------------------------

def test_refresh_fills_the_chat_list(panel):
    panel.refresh_chats()

    assert panel._chat_combo.count() == 2
    assert panel._chat_check.state == "ok"
    assert "2 chats" in panel._chat_check.message


def test_whatsapp_being_unavailable_is_reported(panel, controller):
    controller.chats_available = False
    panel.refresh_chats()

    assert panel._chat_check.state == "bad"
    assert "isn't available" in panel._chat_check.message


def test_both_flows_are_disabled_until_a_chat_is_picked(qapp, controller):
    from winspark.ui.hub_panel import HubPanel

    panel = HubPanel(controller)
    assert panel._webhook.isEnabled() is False
    assert panel._capture.isEnabled() is False
    assert "Pick a chat" in panel._send_status.message


# --- send flow ----------------------------------------------------------------

def test_linking_a_chat_to_a_webhook_starts_it(panel, controller):
    _pick(panel, "Varshith")
    panel._webhook.setText("https://example.com/hook")
    panel.toggle_send()

    link = controller.settings.send_link_for("Varshith")
    assert link.webhook_url == "https://example.com/hook"
    assert link.enabled is True
    assert panel._send_button.text() == "Stop"
    assert panel._send_status.state == "ok"


def test_linking_without_an_address_is_refused(panel, controller):
    _pick(panel, "Varshith")
    panel._webhook.setText("   ")
    panel.toggle_send()

    assert controller.settings.send_link_for("Varshith") is None
    assert panel._send_status.state == "bad"
    assert "web address" in panel._send_status.message


def test_stopping_leaves_the_link_saved_but_paused(panel, controller):
    _pick(panel, "Varshith")
    panel._webhook.setText("https://example.com/hook")
    panel.toggle_send()
    panel.toggle_send()

    link = controller.settings.send_link_for("Varshith")
    assert link.enabled is False
    assert link.webhook_url == "https://example.com/hook"    # remembered
    assert panel._send_button.text() == "Link & start"


def test_unlinking_removes_it(panel, controller):
    _pick(panel, "Varshith")
    panel._webhook.setText("https://example.com/hook")
    panel.toggle_send()
    panel.unlink_send()

    assert controller.settings.send_link_for("Varshith") is None


def test_the_chosen_interval_is_saved(panel, controller):
    _pick(panel, "Varshith")
    panel._webhook.setText("https://example.com/hook")
    panel._interval.setCurrentIndex(panel._interval.findData(30))
    panel.toggle_send()

    assert controller.settings.send_link_for("Varshith").interval_seconds == 30


def test_switching_chats_shows_that_chat_s_own_link(panel, controller):
    controller.settings.set_send_link("Varshith", "https://one.example", enabled=True)
    controller.settings.set_send_link("Nagen US", "https://two.example", enabled=False)
    panel.refresh_chats()

    panel._chat_combo.setCurrentText("Varshith")
    assert panel._webhook.text() == "https://one.example"
    assert panel._send_button.text() == "Stop"

    panel._chat_combo.setCurrentText("Nagen US")
    assert panel._webhook.text() == "https://two.example"
    assert panel._send_button.text() == "Link & start"


# --- save flow ----------------------------------------------------------------

def test_ticking_save_turns_capture_on_for_that_chat(panel, controller):
    _pick(panel, "Varshith")
    panel._capture.setChecked(True)

    assert controller.settings.is_capturing("Varshith") is True
    assert panel._save_status.state == "ok"


def test_unticking_save_turns_it_off(panel, controller):
    _pick(panel, "Varshith")
    panel._capture.setChecked(True)
    panel._capture.setChecked(False)

    assert controller.settings.is_capturing("Varshith") is False


def test_save_follows_the_selected_chat(panel, controller):
    controller.settings.set_capture("Varshith", True)
    panel.refresh_chats()

    panel._chat_combo.setCurrentText("Varshith")
    assert panel._capture.isChecked() is True

    panel._chat_combo.setCurrentText("Nagen US")
    assert panel._capture.isChecked() is False


# --- the two flows are independent -------------------------------------------

def test_linking_a_webhook_does_not_start_saving(panel, controller):
    _pick(panel, "Varshith")
    panel._webhook.setText("https://example.com/hook")
    panel.toggle_send()

    assert controller.settings.is_capturing("Varshith") is False
    assert panel._capture.isChecked() is False


def test_saving_does_not_link_a_webhook(panel, controller):
    _pick(panel, "Varshith")
    panel._capture.setChecked(True)

    assert controller.settings.send_link_for("Varshith") is None
    assert panel._webhook.text() == ""


def test_a_chat_can_do_both_at_once(panel, controller):
    _pick(panel, "Varshith")
    panel._webhook.setText("https://example.com/hook")
    panel.toggle_send()
    panel._capture.setChecked(True)

    assert controller.settings.send_link_for("Varshith").enabled is True
    assert controller.settings.is_capturing("Varshith") is True


# --- MongoDB ------------------------------------------------------------------

def test_the_collection_defaults_to_the_hub_not_chat_memory(panel):
    assert panel._collection.text() == DEFAULT_COLLECTION == "wa_message_hub"


def test_saving_the_connection_reports_success(panel, controller):
    panel._mongo_uri.setText("mongodb://host:27017/mydb")
    panel.save_mongo()

    assert panel._mongo_status.state == "ok"
    assert controller.settings.config.mongo_uri == "mongodb://host:27017/mydb"


def test_a_bad_connection_says_so(panel, controller):
    controller.mongo_ok = False
    panel._mongo_uri.setText("mongodb://dead:27017/mydb")
    panel.save_mongo()

    assert panel._mongo_status.state == "bad"
    assert "No MongoDB server answered" in panel._mongo_status.message


def test_a_blank_collection_falls_back_to_the_default(panel, controller):
    panel._mongo_uri.setText("mongodb://host:27017/mydb")
    panel._collection.setText("   ")
    panel.save_mongo()

    assert controller.settings.config.mongo_collection == DEFAULT_COLLECTION


def test_settings_survive_a_panel_rebuild(qapp, controller):
    """The panel holds no state of its own — config.json/data.json do."""
    from winspark.ui.hub_panel import HubPanel

    first = HubPanel(controller)
    first._spawn = lambda w: w()
    first.refresh_chats()
    first._chat_combo.setCurrentText("Varshith")
    first._webhook.setText("https://example.com/hook")
    first.toggle_send()

    second = HubPanel(FakeHubController(controller.settings.data_path.parent))
    second._spawn = lambda w: w()
    second.refresh_chats()
    second._chat_combo.setCurrentText("Varshith")

    assert second._webhook.text() == "https://example.com/hook"
    assert second._send_button.text() == "Stop"
