"""config.json + data.json as the message hub's only storage.

With no database behind these files, a corrupt or half-written one has nothing
to fall back on — so the tests here care mostly about the failure paths:
unreadable files must degrade to defaults rather than stop the app, and a write
interrupted partway must leave the previous file intact.
"""

import json

import pytest

from winspark.hub.settings_files import (
    DEFAULT_COLLECTION,
    DEFAULT_SPOOL_SECONDS,
    MIN_SPOOL_SECONDS,
    HubSettings,
)


@pytest.fixture
def settings(tmp_path):
    return HubSettings(tmp_path)


# --- config.json --------------------------------------------------------------

def test_defaults_apply_when_nothing_is_saved_yet(settings):
    config = settings.config
    assert config.mongo_uri == ""
    assert config.mongo_collection == DEFAULT_COLLECTION
    assert config.spool_interval_seconds == DEFAULT_SPOOL_SECONDS


def test_the_default_collection_is_the_hub_not_chat_memory():
    """chat_memory belongs to the older AI-memory feature, which is going away;
    the hub must not inherit its lifetime."""
    assert DEFAULT_COLLECTION == "wa_message_hub"


def test_config_survives_a_restart(tmp_path):
    HubSettings(tmp_path).update_config(mongo_uri="mongodb://host/db", mongo_collection="custom")
    reloaded = HubSettings(tmp_path).config

    assert reloaded.mongo_uri == "mongodb://host/db"
    assert reloaded.mongo_collection == "custom"


def test_a_blank_collection_falls_back_rather_than_writing_to_an_unnamed_one(settings):
    assert settings.update_config(mongo_collection="   ").mongo_collection == DEFAULT_COLLECTION


def test_the_spool_interval_cannot_be_set_below_the_floor(settings):
    assert settings.update_config(spool_interval_seconds=0).spool_interval_seconds == MIN_SPOOL_SECONDS
    assert settings.update_config(spool_interval_seconds=1).spool_interval_seconds == MIN_SPOOL_SECONDS
    assert settings.update_config(spool_interval_seconds=30).spool_interval_seconds == 30


def test_a_nonsense_interval_does_not_crash_the_load(tmp_path):
    (tmp_path / "config.json").write_text('{"spool_interval_seconds": "soon"}', encoding="utf-8")
    assert HubSettings(tmp_path).config.spool_interval_seconds == DEFAULT_SPOOL_SECONDS


@pytest.mark.parametrize("contents", [
    "",                       # empty file
    "{",                      # truncated write
    "not json at all",
    "[1, 2, 3]",              # valid JSON, wrong shape
])
def test_an_unusable_config_file_falls_back_to_defaults(tmp_path, contents):
    (tmp_path / "config.json").write_text(contents, encoding="utf-8")
    config = HubSettings(tmp_path).config     # must not raise
    assert config.mongo_collection == DEFAULT_COLLECTION


def test_config_and_data_are_separate_files(tmp_path):
    """A hand-edit of the connection string must not be clobbered by a UI action
    linking a chat a second later."""
    settings = HubSettings(tmp_path)
    settings.update_config(mongo_uri="mongodb://host/db")
    settings.set_send_link("Varshith", "https://example.com/hook", enabled=True)

    assert json.loads(settings.config_path.read_text())["mongo_uri"] == "mongodb://host/db"
    assert "mongo_uri" not in json.loads(settings.data_path.read_text())


# --- data.json: send links ----------------------------------------------------

def test_linking_a_chat_to_a_webhook_persists(tmp_path):
    HubSettings(tmp_path).set_send_link("Varshith", "https://example.com/hook", enabled=True)
    link = HubSettings(tmp_path).send_link_for("Varshith")

    assert link.webhook_url == "https://example.com/hook"
    assert link.enabled is True
    assert link.interval_seconds == DEFAULT_SPOOL_SECONDS


def test_relinking_replaces_rather_than_adding_a_second_link(settings):
    settings.set_send_link("Varshith", "https://one.example", enabled=True)
    settings.set_send_link("Varshith", "https://two.example", enabled=False)

    assert len(settings.data.send_links) == 1
    assert settings.send_link_for("Varshith").webhook_url == "https://two.example"


def test_a_chat_is_matched_regardless_of_case_and_padding(settings):
    settings.set_send_link("Varshith", "https://one.example", enabled=True)
    assert settings.send_link_for("  varshith ") is not None
    settings.set_send_link("  VARSHITH  ", "https://two.example", enabled=True)
    assert len(settings.data.send_links) == 1      # still one chat, not two


def test_only_enabled_links_with_a_url_are_spooled(settings):
    settings.set_send_link("A", "https://a.example", enabled=True)
    settings.set_send_link("B", "https://b.example", enabled=False)
    settings.set_send_link("C", "", enabled=True)          # enabled but nowhere to poll

    assert [l.chat for l in settings.enabled_send_links()] == ["A"]


def test_removing_a_link_persists(tmp_path):
    settings = HubSettings(tmp_path)
    settings.set_send_link("Varshith", "https://example.com/hook", enabled=True)
    settings.remove_send_link("varshith")

    assert settings.send_link_for("Varshith") is None
    assert HubSettings(tmp_path).send_link_for("Varshith") is None


def test_a_link_needs_a_chat_name(settings):
    with pytest.raises(ValueError):
        settings.set_send_link("   ", "https://example.com", enabled=True)


# --- data.json: capture -------------------------------------------------------

def test_capture_is_off_until_turned_on(settings):
    assert settings.is_capturing("Varshith") is False
    settings.set_capture("Varshith", True)
    assert settings.is_capturing("Varshith") is True
    assert settings.capturing_chats() == ("Varshith",)


def test_capture_can_be_turned_back_off_without_losing_the_entry(settings):
    settings.set_capture("Varshith", True)
    settings.set_capture("Varshith", False)

    assert settings.is_capturing("Varshith") is False
    assert settings.capturing_chats() == ()
    assert settings.capture_for("Varshith") is not None    # remembered, just off


def test_send_and_capture_are_independent(settings):
    """The two flows are configured separately — linking a webhook must not
    start saving messages, and vice versa."""
    settings.set_send_link("Varshith", "https://example.com/hook", enabled=True)
    assert settings.is_capturing("Varshith") is False

    settings.set_capture("Nagen US", True)
    assert settings.send_link_for("Nagen US") is None


@pytest.mark.parametrize("contents", ["", "{", "[]", '{"send_links": "nope"}'])
def test_an_unusable_data_file_falls_back_to_empty(tmp_path, contents):
    (tmp_path / "data.json").write_text(contents, encoding="utf-8")
    data = HubSettings(tmp_path).data        # must not raise

    assert data.send_links == ()
    assert data.capture_chats == ()


def test_malformed_rows_are_dropped_but_good_ones_survive(tmp_path):
    (tmp_path / "data.json").write_text(json.dumps({
        "send_links": [
            {"chat": "Good", "webhook_url": "https://ok.example", "enabled": True},
            {"webhook_url": "https://orphan.example"},     # no chat -> unusable
            "not even an object",
        ],
    }), encoding="utf-8")

    links = HubSettings(tmp_path).data.send_links
    assert [l.chat for l in links] == ["Good"]


def test_a_write_leaves_no_temp_file_behind(settings):
    settings.set_send_link("Varshith", "https://example.com/hook", enabled=True)
    settings.update_config(mongo_uri="mongodb://host/db")

    leftovers = list(settings.data_path.parent.glob("*.tmp"))
    assert leftovers == []


def test_an_interrupted_write_does_not_destroy_the_previous_file(tmp_path, monkeypatch):
    """Writes go via a temp file and os.replace precisely so a crash mid-write
    can't truncate the real one — there is no database to recover from."""
    settings = HubSettings(tmp_path)
    settings.set_send_link("Varshith", "https://good.example", enabled=True)
    before = settings.data_path.read_text(encoding="utf-8")

    import winspark.hub.settings_files as sf
    monkeypatch.setattr(sf.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        settings.set_send_link("Varshith", "https://bad.example", enabled=True)

    assert settings.data_path.read_text(encoding="utf-8") == before
    assert HubSettings(tmp_path).send_link_for("Varshith").webhook_url == "https://good.example"
