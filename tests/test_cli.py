"""Tests the management CLI end-to-end by driving cli.main(argv) against a
real temp SQLite database (no subprocess, no mocking). Cross-platform — the
binding/message/relay commands don't touch Windows/UI Automation at all.
"""

import pytest

from winspark.cli import main
from winspark.connectors.fetch_webhook_models import (
    WhatsAppFetchBindingEntity,
    WhatsAppFetchRelayMessageEntity,
    WhatsAppFetchRelayMessageState,
)
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository
from winspark.data.connection import ConnectionFactory


@pytest.fixture
def db(tmp_path):
    return tmp_path / "cli_test.db"


def _run(db, *args) -> int:
    return main(["--db", str(db), *args])


def _repo(db) -> WhatsAppFetchRelayRepository:
    factory = ConnectionFactory(db)
    factory.initialize_schema()
    return WhatsAppFetchRelayRepository(factory)


def test_bindings_list_when_empty(db, capsys):
    assert _run(db, "bindings", "list") == 0
    assert "No bindings" in capsys.readouterr().out


def test_bindings_add_persists_to_db(db, capsys):
    rc = _run(db, "bindings", "add", "Family", "http://localhost:5001/webhook/Family", "--interval", "7")
    assert rc == 0

    bindings = _repo(db).get_bindings()
    assert len(bindings) == 1
    assert bindings[0].group_name == "Family"
    assert bindings[0].poll_interval_seconds == 7
    assert bindings[0].is_enabled is True


def test_bindings_add_blank_url_defaults_to_local_mock(db, capsys):
    _run(db, "bindings", "add", "Family")
    assert _repo(db).get_bindings()[0].fetch_url == "http://localhost:5001/webhook/Family"


def test_bindings_add_rejects_invalid_url(db, capsys):
    rc = _run(db, "bindings", "add", "Family", "not-a-real-url")
    assert rc == 2
    assert "Invalid poll URL" in capsys.readouterr().err
    assert _repo(db).get_bindings() == []


def test_bindings_add_same_group_updates_not_duplicates(db):
    _run(db, "bindings", "add", "Family", "http://localhost:5001/webhook/Family", "--interval", "3")
    _run(db, "bindings", "add", "Family", "http://localhost:5001/webhook/Family", "--interval", "9")
    bindings = _repo(db).get_bindings()
    assert len(bindings) == 1
    assert bindings[0].poll_interval_seconds == 9


def test_bindings_add_disabled_flag(db):
    _run(db, "bindings", "add", "Family", "--disabled")
    assert _repo(db).get_bindings()[0].is_enabled is False


def test_bindings_disable_then_enable_by_group_name(db, capsys):
    _run(db, "bindings", "add", "Family")

    assert _run(db, "bindings", "disable", "Family") == 0
    assert _repo(db).get_bindings()[0].is_enabled is False

    assert _run(db, "bindings", "enable", "Family") == 0
    assert _repo(db).get_bindings()[0].is_enabled is True


def test_bindings_enable_by_binding_id(db):
    _run(db, "bindings", "add", "Family", "--disabled")
    binding_id = _repo(db).get_bindings()[0].binding_id

    assert _run(db, "bindings", "enable", binding_id) == 0
    assert _repo(db).get_bindings()[0].is_enabled is True


def test_bindings_enable_unknown_returns_error(db, capsys):
    assert _run(db, "bindings", "enable", "Nonexistent") == 1
    assert "No binding matching" in capsys.readouterr().err


def test_bindings_remove(db, capsys):
    _run(db, "bindings", "add", "Family")
    assert _run(db, "bindings", "remove", "Family") == 0
    assert _repo(db).get_bindings() == []


def test_bindings_list_shows_added_binding(db, capsys):
    _run(db, "bindings", "add", "Family", "http://localhost:5001/webhook/Family")
    capsys.readouterr()  # clear
    _run(db, "bindings", "list")
    out = capsys.readouterr().out
    assert "Family" in out
    assert "yes" in out  # enabled column


def test_messages_empty(db, capsys):
    assert _run(db, "messages") == 0
    assert "No relayed messages" in capsys.readouterr().out


def test_messages_shows_relayed_history(db, capsys):
    repo = _repo(db)
    binding = WhatsAppFetchBindingEntity(group_name="Family", fetch_url="http://x")
    repo.upsert_binding(binding)
    repo.insert_message(
        WhatsAppFetchRelayMessageEntity(
            binding_id=binding.binding_id,
            message_text="hello from AI",
            content_hash="h1",
            state=WhatsAppFetchRelayMessageState.SENT,
        )
    )
    _run(db, "messages")
    out = capsys.readouterr().out
    assert "hello from AI" in out
    assert "Family" in out
    assert "SENT" in out


def test_relay_status_defaults_to_disabled(db, capsys):
    _run(db, "relay", "status")
    assert "Relay enabled : False" in capsys.readouterr().out


def test_relay_enable_persists_and_status_reflects_it(db, capsys):
    assert _run(db, "relay", "enable") == 0
    capsys.readouterr()
    _run(db, "relay", "status")
    assert "Relay enabled : True" in capsys.readouterr().out

    assert _run(db, "relay", "disable") == 0
    capsys.readouterr()
    _run(db, "relay", "status")
    assert "Relay enabled : False" in capsys.readouterr().out


def test_relay_enabled_flag_is_read_the_same_way_app_reads_it(db):
    # The app reads SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED directly; make sure the
    # CLI writes exactly what the app looks for.
    from winspark.constants import SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED
    from winspark.data.repositories import SettingsRepository

    _run(db, "relay", "enable")
    factory = ConnectionFactory(db)
    factory.initialize_schema()
    value = SettingsRepository(factory).get_value(SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED)
    assert value is not None and value.lower() in ("true", "1")
