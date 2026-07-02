"""Headless tests for the PySide6 control panel.

Runs Qt under the 'offscreen' platform (no display needed) and drives the
window's UI-logic methods against a fake controller backed by a real temp-DB
repository — so table population, relay toggling, and binding CRUD wiring are
all exercised without a real engine, WhatsApp, or a visible window.
"""

import os

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
from winspark.data.connection import ConnectionFactory  # noqa: E402


class FakeController:
    """Same method surface as EngineHost, backed by a real repository but no
    async engine — records relay/inject calls for assertions."""

    def __init__(self, db_path):
        factory = ConnectionFactory(db_path)
        factory.initialize_schema()
        self.repo = WhatsAppFetchRelayRepository(factory)
        self.relay_enabled = False
        self.injected: list[tuple[str, str]] = []
        self.chats = ["Family", "Work Team", "Alice"]

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
                group_name=group,
                fetch_url=normalize_poll_url(url, group),
                api_key=api_key,
                poll_interval_seconds=interval,
                is_enabled=enabled,
            )
        )

    def set_binding_enabled(self, binding_id, enabled):
        self.repo.set_binding_enabled(binding_id, enabled)

    def delete_binding(self, binding_id):
        self.repo.delete_binding(binding_id)

    def inject_test_message(self, group, text):
        self.injected.append((group, text))

    def list_chats(self):
        return self.chats


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def controller(tmp_path):
    return FakeController(tmp_path / "ui_test.db")


@pytest.fixture
def window(qapp, controller):
    from winspark.ui.main_window import MainWindow

    win = MainWindow(controller)
    try:
        yield win
    finally:
        win.close()


def test_window_starts_empty_with_relay_stopped(window):
    assert window._bindings_table.rowCount() == 0
    assert window._relay_button.text() == "Start relay"
    assert "stopped" in window._relay_label.text()


def test_add_binding_populates_the_table(window, controller):
    window.add_binding("Family", "http://localhost:5001/webhook/Family", 5)

    assert window._bindings_table.rowCount() == 1
    assert window._bindings_table.item(0, 0).text() == "Family"
    assert window._bindings_table.item(0, 2).text() == "5s"
    assert window._bindings_table.item(0, 3).text() == "yes"
    assert len(controller.get_bindings()) == 1


def test_toggle_relay_flips_state_and_button(window, controller):
    assert controller.is_relay_enabled() is False
    window.toggle_relay()
    assert controller.is_relay_enabled() is True
    assert window._relay_button.text() == "Stop relay"
    assert "running" in window._relay_label.text()
    window.toggle_relay()
    assert controller.is_relay_enabled() is False


def test_disable_and_enable_selected_binding(window, controller):
    window.add_binding("Family", "", 3)
    window._bindings_table.setCurrentCell(0, 0)

    window.set_selected_enabled(False)
    assert controller.get_bindings()[0].is_enabled is False
    assert window._bindings_table.item(0, 3).text() == "no"

    window.set_selected_enabled(True)
    assert controller.get_bindings()[0].is_enabled is True


def test_remove_selected_binding(window, controller):
    window.add_binding("Family", "", 3)
    window._bindings_table.setCurrentCell(0, 0)

    window.remove_selected()
    assert controller.get_bindings() == []
    assert window._bindings_table.rowCount() == 0


def test_actions_with_no_selection_are_no_ops(window, controller):
    window.add_binding("Family", "", 3)
    window._bindings_table.clearSelection()
    window._bindings_table.setCurrentCell(-1, -1)

    # No row selected -> these should do nothing, not raise.
    window.set_selected_enabled(False)
    window.remove_selected()
    window.send_test_to_selected("hi")

    assert len(controller.get_bindings()) == 1
    assert controller.injected == []


def test_send_test_to_selected_forwards_to_controller(window, controller):
    window.add_binding("Family", "", 3)
    window._bindings_table.setCurrentCell(0, 0)

    window.send_test_to_selected("hello from the test")
    assert controller.injected == [("Family", "hello from the test")]


def test_send_test_ignores_blank_text(window, controller):
    window.add_binding("Family", "", 3)
    window._bindings_table.setCurrentCell(0, 0)
    window.send_test_to_selected("   ")
    assert controller.injected == []


def test_refresh_shows_relayed_messages(window, controller):
    binding = WhatsAppFetchBindingEntity(group_name="Family", fetch_url="http://x")
    controller.repo.upsert_binding(binding)
    controller.repo.insert_message(
        WhatsAppFetchRelayMessageEntity(
            binding_id=binding.binding_id,
            message_text="relayed reply",
            content_hash="h1",
            state=WhatsAppFetchRelayMessageState.SENT,
        )
    )
    window.refresh()

    assert window._messages_table.rowCount() == 1
    assert window._messages_table.item(0, 0).text() == "SENT"
    assert window._messages_table.item(0, 1).text() == "Family"
    assert window._messages_table.item(0, 3).text() == "relayed reply"


def test_add_binding_same_group_updates_row_in_place(window, controller):
    window.add_binding("Family", "", 3)
    window.add_binding("Family", "", 9)  # same group, new interval
    assert window._bindings_table.rowCount() == 1
    assert window._bindings_table.item(0, 2).text() == "9s"


def test_add_binding_dialog_collects_values(qapp):
    from winspark.ui.main_window import AddBindingDialog

    dialog = AddBindingDialog(known_chats=["Family", "Work"])
    dialog._group.setCurrentText("Work")
    dialog._url.setText("http://example.com/hook")
    dialog._interval.setValue(15)
    dialog._api_key.setText("token123")
    dialog._enabled.setChecked(False)

    values = dialog.values()
    assert values == {
        "group": "Work",
        "url": "http://example.com/hook",
        "interval": 15,
        "api_key": "token123",
        "enabled": False,
    }
