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


def test_mongo_store_is_unbounded_and_ignores_keep(mongo_store):
    # MongoDB is the durable full-history store: it keeps EVERY message per chat,
    # ignoring `keep` (which only bounds the local SQLite store), so RAG can draw
    # on the whole conversation.
    for i in range(30):
        mongo_store.append_chat_memory("Manohar", "them", "", f"msg {i}", keep=5)
    all_texts = [t for _, _, t in mongo_store.get_chat_memory("Manohar", limit=1000)]
    assert len(all_texts) == 30
    assert all_texts[0] == "msg 0" and all_texts[-1] == "msg 29"  # oldest NOT trimmed


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


def test_mongo_matches_sqlite_for_a_bounded_read(mongo_store, tmp_path):
    """A bounded read returns the same newest window from either store — so they
    stay interchangeable for the recent-window fetch. They diverge only in what
    they RETAIN: SQLite trims to `keep`, MongoDB keeps everything."""
    sqlite = _sqlite_repo(tmp_path)
    for store in (mongo_store, sqlite):
        store.append_chat_memory("Chat", "them", "Sam", "one", keep=3)
        store.append_chat_memory("Chat", "me", "", "two", keep=3)
        store.append_chat_memory("Chat", "them", "Sam", "three", keep=3)
        store.append_chat_memory("Chat", "me", "", "four", keep=3)   # SQLite trims "one"
    newest_three = [("me", "", "two"), ("them", "Sam", "three"), ("me", "", "four")]
    assert mongo_store.get_chat_memory("Chat", limit=3) == newest_three
    assert sqlite.get_chat_memory("Chat", limit=3) == newest_three
    # SQLite dropped "one"; MongoDB still has the full history.
    assert len(sqlite.get_chat_memory("Chat", limit=100)) == 3
    assert len(mongo_store.get_chat_memory("Chat", limit=100)) == 4


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


# --- Atlas vs local Community Server ------------------------------------------
#
# The same connection-string field takes either, so the connection layer has to
# tell them apart: only SRV needs DNS + TLS + a patient timeout, and only a
# local server can be assumed to answer instantly or not at all.

@pytest.mark.parametrize("uri, local", [
    ("mongodb://localhost:27017", True),
    ("mongodb://127.0.0.1:27017/winspark", True),
    ("mongodb://[::1]:27017", True),
    ("mongodb://user:pass@localhost:27017", True),          # credentials don't hide the host
    ("mongodb+srv://user:pass@cluster0.abcd.mongodb.net/", False),
    ("mongodb+srv://localhost/", False),                    # SRV is remote by definition
    ("mongodb://db.internal.example.com:27017", False),
    ("mongodb://10.0.0.5:27017", False),
])
def test_local_vs_remote_uri_detection(uri, local):
    from winspark.data.chat_memory import LOCAL_TIMEOUT_MS, REMOTE_TIMEOUT_MS, connect_timeout_ms, is_local_uri

    assert is_local_uri(uri) is local
    # A local server gets the fast fail; Atlas gets time to do SRV + TLS.
    assert connect_timeout_ms(uri) == (LOCAL_TIMEOUT_MS if local else REMOTE_TIMEOUT_MS)
    assert LOCAL_TIMEOUT_MS < REMOTE_TIMEOUT_MS


@pytest.mark.parametrize("uri, tls", [
    ("mongodb+srv://user:pass@cluster0.abcd.mongodb.net/", True),   # Atlas is always TLS
    ("mongodb://host:27017/?tls=true", True),
    ("mongodb://host:27017/?ssl=true", True),
    ("mongodb://localhost:27017", False),
])
def test_tls_detection(uri, tls):
    from winspark.data.chat_memory import uses_tls

    assert uses_tls(uri) is tls


@pytest.mark.parametrize("uri, configured, expected", [
    # A database named in the URI wins — that's what mongosh/Compass would use.
    ("mongodb://localhost:27017/Winsparkpro", "winspark", "Winsparkpro"),
    # The real Atlas copy-paste shape names no database -> the setting decides.
    ("mongodb+srv://u:p@c0.abcd.mongodb.net/?retryWrites=true&w=majority", "mine", "mine"),
    ("mongodb+srv://u:p@c0.abcd.mongodb.net/atlasdb?retryWrites=true", "mine", "atlasdb"),
    ("mongodb://localhost:27017", "", "winspark"),          # nothing anywhere -> default
    ("mongodb://localhost:27017/", "  ", "winspark"),
])
def test_database_resolution_prefers_the_uri(uri, configured, expected):
    from winspark.data.chat_memory import resolve_database

    assert resolve_database(uri, configured) == expected


def test_store_writes_to_the_database_named_in_the_uri(monkeypatch):
    """The regression this fixes: a URI ending /Winsparkpro silently wrote to
    the `winspark` database from the settings field instead."""
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    from winspark.data.chat_memory import MongoChatMemoryStore

    store = MongoChatMemoryStore("mongodb://localhost:27017/Winsparkpro", database="winspark")
    store.append_chat_memory("Varshith", "them", "V", "hello")

    assert store.database_name == "Winsparkpro"
    assert store._client["Winsparkpro"]["chat_memory"].count_documents({}) == 1
    assert store._client["winspark"]["chat_memory"].count_documents({}) == 0


def test_connection_problems_are_explained_in_plain_english():
    """Atlas fails in ways a bare "couldn't connect" gives no way to fix."""
    from winspark.data.chat_memory import describe_connection_problem

    atlas = "mongodb+srv://u:p@c0.abcd.mongodb.net/"

    class ServerSelectionTimeoutError(Exception):
        pass

    class OperationFailure(Exception):
        pass

    class ConfigurationError(Exception):
        pass

    # The #1 Atlas gotcha: the cluster is fine, your IP just isn't allowed in.
    assert "allowlist" in describe_connection_problem(
        ServerSelectionTimeoutError("No servers found yet"), atlas)
    # The same exception against a local server means something else entirely.
    assert "mongod running" in describe_connection_problem(
        ServerSelectionTimeoutError("No servers found yet"), "mongodb://localhost:27017")
    assert "username and password" in describe_connection_problem(
        OperationFailure("bad auth : Authentication failed."), atlas)
    # A mongodb+srv:// URI without dnspython fails no matter how correct it is.
    assert "dnspython" in describe_connection_problem(
        ConfigurationError('The "dnspython" module must be installed'), atlas)
    assert "pymongo" in describe_connection_problem(ImportError("No module named 'pymongo'"), atlas)

    # DNS failures reach us wrapped in ConfigurationError. Calling the
    # connection string invalid would be wrong AND misleading — it's usually
    # fine and the network is the problem. This is the exact message pymongo
    # produced in a live run against an unreachable SRV host.
    dns_timeout = ConfigurationError(
        "The resolution lifetime expired after 20.007 seconds: "
        "Server Do53:192.168.4.1@53 answered The DNS operation timed out.")
    explained = describe_connection_problem(dns_timeout, atlas)
    assert "DNS" in explained
    assert "isn't valid" not in explained
    assert "doesn't exist" in describe_connection_problem(
        ConfigurationError("... nxdomain ..."), atlas)


def test_srv_lookup_is_bounded_by_connect_timeout(monkeypatch):
    """For mongodb+srv://, pymongo resolves DNS in the MongoClient constructor
    BEFORE server selection — bounded by connectTimeoutMS, not by
    serverSelectionTimeoutMS. Measured live: without connectTimeoutMS an
    unresolvable Atlas host blocked for 20.6s instead of the intended 8s."""
    import importlib.util

    import pymongo
    from winspark.data.chat_memory import REMOTE_TIMEOUT_MS, MongoChatMemoryStore

    seen: dict = {}

    def fake_client(uri, **kwargs):
        seen.update(kwargs)
        # Deliberately NOT handing the +srv URI on: mongomock resolves SRV for
        # real, which would make this test depend on DNS and the network.
        return mongomock.MongoClient("mongodb://fake/")

    monkeypatch.setattr(pymongo, "MongoClient", fake_client)
    MongoChatMemoryStore("mongodb+srv://u:p@c0.abcde.mongodb.net/", database="winspark_test")

    assert seen["connectTimeoutMS"] == REMOTE_TIMEOUT_MS
    assert seen["serverSelectionTimeoutMS"] == REMOTE_TIMEOUT_MS
    if importlib.util.find_spec("certifi"):
        assert "tlsCAFile" in seen   # Atlas is TLS; certifi's bundle verifies it


def test_local_uri_gets_the_fast_timeout_and_no_tls(monkeypatch):
    import pymongo
    from winspark.data.chat_memory import LOCAL_TIMEOUT_MS, MongoChatMemoryStore

    seen: dict = {}

    def fake_client(uri, **kwargs):
        seen.update(kwargs)
        return mongomock.MongoClient(uri)

    monkeypatch.setattr(pymongo, "MongoClient", fake_client)
    MongoChatMemoryStore("mongodb://localhost:27017", database="winspark_test")

    assert seen["serverSelectionTimeoutMS"] == LOCAL_TIMEOUT_MS
    assert "tlsCAFile" not in seen   # a plain local connection isn't TLS


def test_builder_reports_why_it_fell_back(tmp_path, monkeypatch):
    from winspark.data.chat_memory import try_build_chat_memory_store

    sqlite = _sqlite_repo(tmp_path)

    class ServerSelectionTimeoutError(Exception):
        pass

    def boom(*a, **k):
        raise ServerSelectionTimeoutError("No servers found yet")

    import winspark.data.chat_memory as cm
    monkeypatch.setattr(cm, "MongoChatMemoryStore", boom)

    store, problem = try_build_chat_memory_store("mongodb+srv://u:p@c0.abcd.mongodb.net/", sqlite)
    assert store is sqlite
    assert "allowlist" in problem          # actionable, not just "failed"

    # No URI at all is the default, not a failure worth reporting.
    assert try_build_chat_memory_store("", sqlite) == (sqlite, "")


def test_mirror_exposes_the_database_in_use(tmp_path, monkeypatch):
    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", mongomock.MongoClient)
    from winspark.data.chat_memory import build_chat_memory_store

    sqlite = _sqlite_repo(tmp_path)
    store = build_chat_memory_store("mongodb://localhost:27017/Winsparkpro", sqlite, database="winspark")
    assert store.database_name == "Winsparkpro"


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
