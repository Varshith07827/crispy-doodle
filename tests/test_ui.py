"""Headless tests for the PySide6 control panel (all four tabs + the window).

Runs Qt under the 'offscreen' platform and drives each panel's UI-logic methods
against a fake controller backed by a real temp-DB repository for bindings/
messages and in-memory fixtures for chats/windows/events — no real engine,
WhatsApp, display, or STA thread.
"""

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from winspark.connectors.fetch_webhook_models import (  # noqa: E402
    WhatsAppFetchBindingEntity,
    WhatsAppFetchRelayMessageEntity,
    WhatsAppFetchRelayMessageState,
)
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository  # noqa: E402
from winspark.connectors.fetch_webhook_url import normalize_poll_url  # noqa: E402
from winspark.connectors.models import WhatsAppChatRow  # noqa: E402
from winspark.data.connection import ConnectionFactory  # noqa: E402
from winspark.domain.entities import EventEntity  # noqa: E402
from winspark.domain.enums import EventTypeKind, WindowStateKind  # noqa: E402
from winspark.domain.models import WindowInfo  # noqa: E402


class FakeController:
    """Same method surface as EngineHost, backed by a real repository for
    bindings/messages and simple in-memory fixtures for everything else."""

    def __init__(self, db_path):
        factory = ConnectionFactory(db_path)
        factory.initialize_schema()
        self.repo = WhatsAppFetchRelayRepository(factory)
        self.relay_enabled = False
        self.injected: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.chats = [
            WhatsAppChatRow(chat_name="Family", timestamp_text="", last_message="see you soon", unread_count=2, raw_text="Family"),
            WhatsAppChatRow(chat_name="Work", timestamp_text="", last_message="ok", unread_count=0, raw_text="Work"),
        ]
        self.chats_available = True
        self.windows = [
            WindowInfo(handle=1, title="Notepad", process_name="notepad.exe", process_id=100,
                       memory_bytes=52_428_800, window_state=WindowStateKind.NORMAL, is_active=True),
            WindowInfo(handle=2, title="Chrome", process_name="chrome.exe", process_id=200,
                       memory_bytes=314_572_800, window_state=WindowStateKind.MAXIMIZED, is_active=False),
        ]
        self.events = [
            EventEntity(event_type=EventTypeKind.WINDOW_OPENED, process_name="notepad.exe",
                        window_title="Notepad", occurred_at_utc=datetime.now(timezone.utc)),
        ]

    # relay
    def get_bindings(self):
        return self.repo.get_bindings()

    def get_recent_messages(self, limit=30):
        return self.repo.get_recent_messages(limit)

    def is_relay_enabled(self):
        return self.relay_enabled

    def set_relay_enabled(self, enabled):
        self.relay_enabled = enabled

    def add_or_update_binding(self, group, url, interval, api_key="", enabled=True):
        existing = next((b for b in self.repo.get_bindings() if b.group_name.lower() == group.lower()), None)
        self.repo.upsert_binding(
            WhatsAppFetchBindingEntity(
                binding_id=existing.binding_id if existing else WhatsAppFetchBindingEntity().binding_id,
                group_name=group, fetch_url=normalize_poll_url(url, group),
                api_key=api_key, poll_interval_seconds=interval, is_enabled=enabled,
            )
        )

    def set_binding_enabled(self, binding_id, enabled):
        self.repo.set_binding_enabled(binding_id, enabled)

    def delete_binding(self, binding_id):
        self.repo.delete_binding(binding_id)

    def inject_test_message(self, group, text):
        self.injected.append((group, text))

    def list_chats(self):
        return [c.chat_name for c in self.chats] if self.chats_available else None

    # whatsapp
    def get_whatsapp_chats(self):
        return list(self.chats) if self.chats_available else None

    def is_whatsapp_running(self):
        return self.chats_available and bool(self.chats)

    def send_to_chat(self, group, text):
        self.sent.append((group, text))
        return True, "sent"

    # observation
    def get_windows(self):
        return list(self.windows)

    def get_recent_events(self, limit=100):
        return list(self.events)[:limit]


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(tmp_path):
    return FakeController(tmp_path / "ui.db")


# --- RelayPanel ------------------------------------------------------------

@pytest.fixture
def relay(qapp, controller):
    from winspark.ui.panels import RelayPanel

    panel = RelayPanel(controller)
    panel.refresh()
    return panel


def test_relay_starts_empty_and_stopped(relay):
    assert relay._bindings_table.rowCount() == 0
    assert relay._relay_button.text() == "Start relay"


def test_relay_add_binding_populates_table(relay, controller):
    relay.add_binding("Family", "http://localhost:5001/webhook/Family", 5)
    assert relay._bindings_table.rowCount() == 1
    assert relay._bindings_table.item(0, 0).text() == "Family"
    assert relay._bindings_table.item(0, 2).text() == "5s"
    assert len(controller.get_bindings()) == 1


def test_relay_toggle(relay, controller):
    relay.toggle_relay()
    assert controller.is_relay_enabled() is True
    assert relay._relay_button.text() == "Stop relay"


def test_relay_enable_disable_remove_selected(relay, controller):
    relay.add_binding("Family", "", 3)
    relay._bindings_table.setCurrentCell(0, 0)
    relay.set_selected_enabled(False)
    assert controller.get_bindings()[0].is_enabled is False
    relay.remove_selected()
    assert controller.get_bindings() == []


def test_relay_send_test_forwards(relay, controller):
    relay.add_binding("Family", "", 3)
    relay._bindings_table.setCurrentCell(0, 0)
    relay.send_test_to_selected("hi there")
    assert controller.injected == [("Family", "hi there")]


def test_relay_no_selection_is_noop(relay, controller):
    relay.add_binding("Family", "", 3)
    relay._bindings_table.setCurrentCell(-1, -1)
    relay.set_selected_enabled(False)
    relay.remove_selected()
    relay.send_test_to_selected("x")
    assert len(controller.get_bindings()) == 1
    assert controller.injected == []


def test_relay_shows_messages(relay, controller):
    b = WhatsAppFetchBindingEntity(group_name="Family", fetch_url="http://x")
    controller.repo.upsert_binding(b)
    controller.repo.insert_message(WhatsAppFetchRelayMessageEntity(
        binding_id=b.binding_id, message_text="reply", content_hash="h", state=WhatsAppFetchRelayMessageState.SENT))
    relay.refresh()
    assert relay._messages_table.item(0, 0).text() == "SENT"
    assert relay._messages_table.item(0, 3).text() == "reply"


# --- WhatsAppPanel ---------------------------------------------------------

@pytest.fixture
def whatsapp(qapp, controller):
    from winspark.ui.panels import WhatsAppPanel

    panel = WhatsAppPanel(controller)
    panel.refresh()
    return panel


def test_whatsapp_lists_chats_with_unread(whatsapp):
    assert whatsapp._chats_table.rowCount() == 2
    assert whatsapp._chats_table.item(0, 0).text() == "Family"
    assert whatsapp._chats_table.item(0, 1).text() == "2"       # unread
    assert whatsapp._chats_table.item(1, 1).text() == ""        # 0 unread shown blank
    assert whatsapp._status.text() == "2 chats visible, 1 unread"  # 1 chat has unread


def test_whatsapp_unavailable_platform(qapp, controller):
    from winspark.ui.panels import WhatsAppPanel

    controller.chats_available = False
    panel = WhatsAppPanel(controller)
    panel.refresh()
    assert panel._chats_table.rowCount() == 0
    assert "unavailable" in panel._status.text().lower()


def test_whatsapp_not_running(qapp, controller):
    from winspark.ui.panels import WhatsAppPanel

    controller.chats = []
    panel = WhatsAppPanel(controller)
    panel.refresh()
    assert "not running" in panel._status.text().lower()


def test_whatsapp_create_binding_for_chat(whatsapp, controller):
    whatsapp.create_binding_for("Family")
    assert any(b.group_name == "Family" for b in controller.get_bindings())


def test_whatsapp_send_to_chat(whatsapp, controller):
    ok, detail = whatsapp.send_to("Family", "hello")
    assert ok is True
    assert controller.sent == [("Family", "hello")]


def test_whatsapp_selected_chat(whatsapp):
    whatsapp._chats_table.setCurrentCell(1, 0)
    assert whatsapp.selected_chat().chat_name == "Work"


# --- WindowsPanel / EventsPanel -------------------------------------------

def test_windows_panel_lists_windows(qapp, controller):
    from winspark.ui.panels import WindowsPanel

    panel = WindowsPanel(controller)
    panel.refresh()
    assert panel._table.rowCount() == 2
    assert panel._table.item(0, 0).text() == "Notepad"
    assert panel._table.item(0, 1).text() == "notepad.exe"
    assert panel._table.item(0, 5).text() == "●"     # active marker
    assert panel._table.item(1, 4).text() == "Maximized"


def test_events_panel_lists_events(qapp, controller):
    from winspark.ui.panels import EventsPanel

    panel = EventsPanel(controller)
    panel.refresh()
    assert panel._table.rowCount() == 1
    assert panel._table.item(0, 1).text() == "WINDOW_OPENED"
    assert panel._table.item(0, 2).text() == "notepad.exe"


# --- MainWindow ------------------------------------------------------------

@pytest.fixture
def window(qapp, controller):
    from winspark.ui.main_window import MainWindow

    win = MainWindow(controller)
    try:
        yield win
    finally:
        win.close()


def test_main_window_has_four_tabs(window):
    assert window._tabs.count() == 4
    assert [window._tabs.tabText(i) for i in range(4)] == ["AI Relay", "WhatsApp", "Windows", "Events"]


def test_main_window_status_bar_summarizes_state(window, controller):
    controller.add_or_update_binding("Family", "", 3)
    window.refresh()
    msg = window.statusBar().currentMessage()
    assert "Relay: off" in msg
    assert "Bindings: 1" in msg
    assert "Windows: 2" in msg


def test_main_window_tab_switch_refreshes_panel(window):
    window._tabs.setCurrentIndex(2)  # Windows tab
    assert window.windows_panel._table.rowCount() == 2
