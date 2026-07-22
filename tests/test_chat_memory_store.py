"""Chat memory stores: the MongoDB store must behave identically to the SQLite
one (append/trim/read/clear/list), and the store BUILDER must fall back to
SQLite whenever MongoDB is missing or unreachable — the app can't hard-depend
on a running Mongo server.

The Mongo tests run against mongomock (an in-memory pymongo stand-in); they
skip if it isn't installed, so the suite still runs on a bare environment.
"""

import pytest

from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository
from winspark.data.chat_memory import build_chat_memory_store
from winspark.data.connection import ConnectionFactory

mongomock = pytest.importorskip("mongomock")


@pytest.fixture
def mongo_store(monkeypatch):
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    from winspark.data.chat_memory import MongoChatMemoryStore

    return MongoChatMemoryStore("mongodb://fake/", database="winspark_test")


def _sqlite_repo(tmp_path):
    factory = ConnectionFactory(tmp_path / "mem.db")
    factory.initialize_schema()
    return WhatsAppFetchRelayRepository(factory)


def test_mongo_store_appends_and_reads_oldest_first(mongo_store):
    mongo_store.append_chat_memory("Manohar", "them", "Manohar", "hi, I'm Dan")
    mongo_store.append_chat_memory("Manohar", "me", "", "hey Dan!")
    mongo_store.append_chat_memory("Other", "them", "", "unrelated")

    assert mongo_store.get_chat_memory("Manohar") == [
        ("them", "Manohar", "hi, I'm Dan"),
        ("me", "", "hey Dan!"),
    ]
    assert mongo_store.get_chat_memory("Other") == [("them", "", "unrelated")]


def test_mongo_store_keeps_only_the_newest_k(mongo_store):
    for i in range(30):
        mongo_store.append_chat_memory("Manohar", "them", "", f"msg {i}", keep=5)
    assert [t for _, _, t in mongo_store.get_chat_memory("Manohar")] == \
        ["msg 25", "msg 26", "msg 27", "msg 28", "msg 29"]


def test_mongo_store_ignores_blank_and_clears(mongo_store):
    mongo_store.append_chat_memory("", "them", "", "no group")
    mongo_store.append_chat_memory("Manohar", "them", "", "   ")
    assert mongo_store.get_chat_memory("Manohar") == []

    mongo_store.append_chat_memory("Manohar", "them", "", "remember me")
    mongo_store.clear_chat_memory("Manohar")
    assert mongo_store.get_chat_memory("Manohar") == []


def test_mongo_store_lists_chats_with_counts(mongo_store):
    mongo_store.append_chat_memory("A", "them", "", "1")
    mongo_store.append_chat_memory("A", "me", "", "2")
    mongo_store.append_chat_memory("B", "them", "", "1")

    chats = dict(mongo_store.get_chats_with_memory())
    assert chats == {"A": 2, "B": 1}


def test_mongo_matches_sqlite_behaviour(mongo_store, tmp_path):
    """The two stores are interchangeable — same ops, same reads."""
    sqlite = _sqlite_repo(tmp_path)
    for store in (mongo_store, sqlite):
        store.append_chat_memory("Chat", "them", "Sam", "one", keep=3)
        store.append_chat_memory("Chat", "me", "", "two", keep=3)
        store.append_chat_memory("Chat", "them", "Sam", "three", keep=3)
        store.append_chat_memory("Chat", "me", "", "four", keep=3)   # trims "one"
    assert mongo_store.get_chat_memory("Chat") == sqlite.get_chat_memory("Chat")
    assert mongo_store.get_chat_memory("Chat") == [
        ("me", "", "two"), ("them", "Sam", "three"), ("me", "", "four"),
    ]


def test_builder_uses_sqlite_when_no_uri(tmp_path):
    sqlite = _sqlite_repo(tmp_path)
    assert build_chat_memory_store("", sqlite) is sqlite
    assert build_chat_memory_store("   ", sqlite) is sqlite


def test_builder_falls_back_when_mongo_unreachable(tmp_path, monkeypatch):
    sqlite = _sqlite_repo(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("no server here")

    import winspark.data.chat_memory as cm
    monkeypatch.setattr(cm, "MongoChatMemoryStore", boom)

    # A URI is set but the server is dead -> silently use SQLite, never raise.
    assert build_chat_memory_store("mongodb://dead/", sqlite) is sqlite


def test_builder_uses_mongo_when_reachable(tmp_path, monkeypatch):
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    sqlite = _sqlite_repo(tmp_path)

    from winspark.data.chat_memory import MongoChatMemoryStore
    store = build_chat_memory_store("mongodb://fake/", sqlite, database="winspark_test")
    assert isinstance(store, MongoChatMemoryStore)
