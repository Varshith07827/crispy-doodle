"""Writing messages straight to MongoDB's wa_message_hub collection.

Two things separate this from the chat-memory store it replaces: writes are
idempotent (the reader re-offers whatever is on screen every few seconds, so
re-capture must be free rather than the source of duplicates), and there is no
local mirror (a failed write means the message is GONE, so it must be reported
rather than swallowed).
"""

import pytest

mongomock = pytest.importorskip("mongomock")

from winspark.hub.message_hub import (
    HubMessage,
    MessageHubStore,
    build_message_hub,
    message_fingerprint,
)


@pytest.fixture
def store(monkeypatch):
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    return MessageHubStore("mongodb://fake/hubtest", collection="wa_message_hub")


def _msg(text, chat="Varshith", direction="in", sender="V", time_text="1:54 pm", **kw):
    return HubMessage(chat=chat, direction=direction, sender=sender, text=text,
                      time_text=time_text, **kw)


# --- where it writes ----------------------------------------------------------

def test_it_writes_to_wa_message_hub_not_chat_memory(store):
    store.save(_msg("hello"))

    assert store.collection_name == "wa_message_hub"
    assert store._client[store.database_name]["wa_message_hub"].count_documents({}) == 1
    assert store._client[store.database_name]["chat_memory"].count_documents({}) == 0


def test_the_database_comes_from_the_connection_string(store):
    assert store.database_name == "hubtest"


def test_a_custom_collection_is_honoured(monkeypatch):
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    store = MessageHubStore("mongodb://fake/hubtest", collection="something_else")
    assert store.collection_name == "something_else"


# --- what it stores -----------------------------------------------------------

def test_a_stored_message_keeps_every_field(store):
    store.save(_msg("[Voice note · 0:04]", media_kind="voice", media_note="0:04"))
    doc = store.recent("Varshith")[0]

    assert doc["chat"] == "Varshith"
    assert doc["direction"] == "in"
    assert doc["sender"] == "V"
    assert doc["text"] == "[Voice note · 0:04]"
    assert doc["media_kind"] == "voice"
    assert doc["media_note"] == "0:04"
    assert doc["message_time"] == "1:54 pm"
    assert doc["captured_utc"]


def test_direction_separates_sent_from_received(store):
    store.save(_msg("you around?", direction="in"))
    store.save(_msg("yes", direction="out", sender=""))

    assert [d["direction"] for d in store.recent("Varshith")] == ["in", "out"]


def test_chats_are_kept_apart(store):
    store.save(_msg("hi", chat="Varshith"))
    store.save(_msg("hi", chat="Nagen US"))

    assert store.count_for("Varshith") == 1
    assert store.count_for("Nagen US") == 1


def test_blank_messages_are_not_stored(store):
    assert store.save(_msg("   ")) is False
    assert store.save(_msg("hi", chat="  ")) is False
    assert store.count_for("Varshith") == 0


# --- idempotence: the reader re-offers the same screen every few seconds ------

def test_saving_the_same_message_repeatedly_stores_it_once(store):
    message = _msg("you around?")
    for _ in range(10):
        assert store.save(message) is True

    assert store.count_for("Varshith") == 1


def test_the_same_words_at_a_different_time_are_a_different_message(store):
    """The mistake chat memory made: identity was the text alone, so asking the
    same question twice was discarded as a duplicate for ever."""
    store.save(_msg("who wrote the national anthem", time_text="1:00 pm"))
    store.save(_msg("who wrote the national anthem", time_text="1:02 pm"))

    assert store.count_for("Varshith") == 2


def test_the_same_words_from_different_people_are_different_messages(store):
    store.save(_msg("ok", sender="Ravi"))
    store.save(_msg("ok", sender="Asha"))

    assert store.count_for("Varshith") == 2


def test_fingerprints_are_stable_across_runs():
    a = message_fingerprint("Varshith", "in", "V", "hello", "1:54 pm")
    b = message_fingerprint(" varshith ", "IN", " v ", "hello", "1:54 PM")
    assert a == b                                          # normalised
    assert a != message_fingerprint("Varshith", "out", "V", "hello", "1:54 pm")


# --- no local mirror: a failure is a loss, and must be reported ---------------

def test_a_failed_write_is_reported_not_swallowed(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("server went away")

    monkeypatch.setattr(store._col, "update_one", boom)

    assert store.save(_msg("this will not land")) is False
    assert "server went away" in store.last_error


def test_a_failure_does_not_raise_into_the_capture_pass(store, monkeypatch):
    monkeypatch.setattr(store._col, "update_one",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    store.save(_msg("one"))          # must not raise


def test_a_later_success_clears_the_error(store, monkeypatch):
    original = store._col.update_one
    monkeypatch.setattr(store._col, "update_one",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    store.save(_msg("one"))
    assert store.last_error

    monkeypatch.setattr(store._col, "update_one", original)
    assert store.save(_msg("two")) is True
    assert store.last_error == ""


def test_save_many_reports_how_many_landed(store):
    stored, failed = store.save_many([_msg("a"), _msg("b"), _msg("   ")])
    assert (stored, failed) == (2, 1)


# --- from the connector's message type ---------------------------------------

class _WhatsAppMessage:
    def __init__(self, sender, text, is_incoming, media_kind="", media_note="", time_text=""):
        self.sender, self.text, self.is_incoming = sender, text, is_incoming
        self.media_kind, self.media_note, self.time_text = media_kind, media_note, time_text


def test_it_converts_a_connector_message(store):
    incoming = _WhatsAppMessage("Varshith", "you around?", True, time_text="1:54 pm")
    outgoing = _WhatsAppMessage("", "yes", False, time_text="1:55 pm")

    store.save(HubMessage.from_whatsapp("Varshith", incoming))
    store.save(HubMessage.from_whatsapp("Varshith", outgoing))

    docs = store.recent("Varshith")
    assert [(d["direction"], d["sender"], d["text"]) for d in docs] == [
        ("in", "Varshith", "you around?"),
        ("out", "", "yes"),
    ]


def test_a_media_message_carries_its_kind(store):
    photo = _WhatsAppMessage("V", "[Photo]", True, media_kind="photo", time_text="1:54 pm")
    store.save(HubMessage.from_whatsapp("Varshith", photo))
    assert store.recent("Varshith")[0]["media_kind"] == "photo"


# --- the builder --------------------------------------------------------------

def test_the_builder_explains_a_missing_connection_string():
    store, problem = build_message_hub("")
    assert store is None
    assert "connection string" in problem


def test_the_builder_explains_an_unreachable_server(monkeypatch):
    import winspark.hub.message_hub as hub

    class _Boom(Exception):
        pass

    def boom(*a, **k):
        raise _Boom("No servers found yet")

    monkeypatch.setattr(hub, "MessageHubStore", boom)
    store, problem = build_message_hub("mongodb://dead:27017/db")

    assert store is None
    assert "mongod running" in problem or "reach" in problem
