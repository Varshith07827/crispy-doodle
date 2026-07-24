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

    from winspark.data.chat_memory import MirroredChatMemoryStore, MongoChatMemoryStore
    store = build_chat_memory_store("mongodb://fake/", sqlite, database="winspark_test")
    # Mongo is reachable -> a MIRROR of Mongo (primary) + the SQLite fallback,
    # so the two stores never diverge.
    assert isinstance(store, MirroredChatMemoryStore)
    assert isinstance(store.primary, MongoChatMemoryStore)
    assert store.mirror is sqlite


def test_mirror_writes_to_both_and_reads_from_primary(tmp_path, monkeypatch):
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    sqlite = _sqlite_repo(tmp_path)
    store = build_chat_memory_store("mongodb://fake/", sqlite, database="winspark_test")

    store.append_chat_memory("Manohar", "them", "Dan", "hi")
    store.append_chat_memory("Manohar", "me", "", "hey")

    expected = [("them", "Dan", "hi"), ("me", "", "hey")]
    assert store.get_chat_memory("Manohar") == expected          # via primary (Mongo)
    assert store.primary.get_chat_memory("Manohar") == expected  # landed in Mongo
    assert sqlite.get_chat_memory("Manohar") == expected         # AND the local mirror

    store.clear_chat_memory("Manohar")
    assert store.primary.get_chat_memory("Manohar") == []        # cleared in both
    assert sqlite.get_chat_memory("Manohar") == []


def test_mirror_reconcile_unions_both_stores(tmp_path, monkeypatch):
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    from winspark.data.chat_memory import MirroredChatMemoryStore, MongoChatMemoryStore

    sqlite = _sqlite_repo(tmp_path)
    sqlite.append_chat_memory("LocalOnly", "them", "", "from sqlite")   # only local
    mongo = MongoChatMemoryStore("mongodb://fake/", database="winspark_test")
    mongo.append_chat_memory("MongoOnly", "them", "", "from mongo")     # only mongo

    mirror = MirroredChatMemoryStore(primary=mongo, mirror=sqlite)
    moved_up = mirror.reconcile()

    assert moved_up == 1                                        # LocalOnly carried up to Mongo
    # After reconcile, BOTH stores hold BOTH chats — no more "shows in one only".
    assert dict(mongo.get_chats_with_memory()) == {"LocalOnly": 1, "MongoOnly": 1}
    assert dict(sqlite.get_chats_with_memory()) == {"LocalOnly": 1, "MongoOnly": 1}


def test_copy_migrates_all_chats_and_skips_existing(tmp_path, monkeypatch):
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    from winspark.data.chat_memory import MongoChatMemoryStore, copy_chat_memory

    sqlite = _sqlite_repo(tmp_path)
    sqlite.append_chat_memory("Manohar", "them", "Dan", "hi")
    sqlite.append_chat_memory("Manohar", "me", "", "hey")
    sqlite.append_chat_memory("Sharon", "them", "", "yo")

    mongo = MongoChatMemoryStore("mongodb://fake/", database="winspark_test")
    mongo.append_chat_memory("Sharon", "me", "", "already here")   # Sharon pre-exists in Mongo

    moved = copy_chat_memory(sqlite, mongo)

    assert moved == 2                                  # only Manohar's 2 (Sharon skipped)
    assert mongo.get_chat_memory("Manohar") == [("them", "Dan", "hi"), ("me", "", "hey")]
    assert mongo.get_chat_memory("Sharon") == [("me", "", "already here")]   # untouched


# --- attachment metadata + timestamp persistence -----------------------------

def test_sqlite_rich_read_carries_media_and_time(tmp_path):
    repo = _sqlite_repo(tmp_path)
    repo.append_chat_memory("Fam", "them", "Vishnu", "[Voice note · 0:12]",
                            media_kind="voice", media_note="0:12", time_text="9:21 pm")
    repo.append_chat_memory("Fam", "them", "Vishnu", "[Photo] look",
                            media_kind="photo", media_note="look",
                            media_path=r"C:\media\x.png", time_text="9:22 pm")
    repo.append_chat_memory("Fam", "me", "", "nice")  # plain, no media kwargs

    rich = repo.get_chat_memory_rich("Fam")
    assert rich[0] == {"role": "them", "sender": "Vishnu", "text": "[Voice note · 0:12]",
                       "media_kind": "voice", "media_note": "0:12", "media_path": "",
                       "time_text": "9:21 pm"}
    assert rich[1]["media_kind"] == "photo" and rich[1]["media_path"] == r"C:\media\x.png"
    assert rich[2]["media_kind"] == "" and rich[2]["time_text"] == ""
    # The plain 3-tuple read still works unchanged (placeholder rides in text).
    assert repo.get_chat_memory("Fam")[0] == ("them", "Vishnu", "[Voice note · 0:12]")


def test_mongo_rich_read_carries_media_and_time(mongo_store):
    mongo_store.append_chat_memory("Fam", "them", "V", "[Document: report.pdf]",
                                   media_kind="document", media_note="report.pdf", time_text="8:00 am")
    row = mongo_store.get_chat_memory_rich("Fam")[0]
    assert row["media_kind"] == "document"
    assert row["media_note"] == "report.pdf"
    assert row["time_text"] == "8:00 am"


def test_copy_between_stores_preserves_media(tmp_path, monkeypatch):
    import pymongo
    from winspark.data.chat_memory import MongoChatMemoryStore, copy_chat_memory

    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    src = _sqlite_repo(tmp_path)
    src.append_chat_memory("Fam", "them", "V", "[Photo] hi", media_kind="photo",
                           media_note="hi", media_path=r"C:\m\y.png", time_text="1:00 pm")
    dst = MongoChatMemoryStore("mongodb://fake/", database="winspark_test")

    copied = copy_chat_memory(src, dst)
    assert copied == 1
    row = dst.get_chat_memory_rich("Fam")[0]
    assert row["media_kind"] == "photo"
    assert row["media_path"] == r"C:\m\y.png"
    assert row["time_text"] == "1:00 pm"
