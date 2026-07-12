"""Tests WhatsAppFetchRelayRepository against a real SQLite database (same
pattern as test_schema.py) — cross-platform."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from winspark.connectors.fetch_webhook_models import (
    WhatsAppFetchBindingEntity,
    WhatsAppFetchRelayMessageEntity,
    WhatsAppFetchRelayMessageState,
)
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository, compute_content_hash
from winspark.data.connection import ConnectionFactory


def _make_repo(tmp_path) -> WhatsAppFetchRelayRepository:
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()
    return WhatsAppFetchRelayRepository(factory)


def test_upsert_and_get_binding_round_trip(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://localhost:5001/webhook/Infosys", poll_interval_seconds=5)

    repo.upsert_binding(binding)
    fetched = repo.get_binding(binding.binding_id)

    assert fetched is not None
    assert fetched.group_name == "Infosys"
    assert fetched.poll_interval_seconds == 5
    assert fetched.is_enabled is True


def test_upsert_is_an_update_when_binding_id_matches(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x/1")
    repo.upsert_binding(binding)

    updated = WhatsAppFetchBindingEntity(binding_id=binding.binding_id, group_name="Infosys Team", fetch_url="http://x/2")
    repo.upsert_binding(updated)

    fetched = repo.get_binding(binding.binding_id)
    assert fetched.group_name == "Infosys Team"
    assert fetched.fetch_url == "http://x/2"
    assert len(repo.get_bindings()) == 1


def test_delete_binding_also_deletes_its_messages(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)
    repo.insert_message(WhatsAppFetchRelayMessageEntity(binding_id=binding.binding_id, message_text="hi", content_hash="h1"))

    repo.delete_binding(binding.binding_id)

    assert repo.get_binding(binding.binding_id) is None
    assert repo.get_recent_messages() == []


def test_update_binding_status_sets_last_error_and_state(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)

    repo.update_binding_status(binding.binding_id, "error", last_fetch_utc=datetime.now(timezone.utc), last_error="boom")

    fetched = repo.get_binding(binding.binding_id)
    assert fetched.last_fetch_state == "error"
    assert fetched.last_error == "boom"


def test_send_failed_status_clears_last_send_utc(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)
    repo.update_binding_status(binding.binding_id, "sent-verified", last_send_utc=datetime.now(timezone.utc))
    assert repo.get_binding(binding.binding_id).last_send_utc is not None

    repo.update_binding_status(binding.binding_id, "send-failed", last_error="failed")
    assert repo.get_binding(binding.binding_id).last_send_utc is None


def test_increment_poll_and_sent_counts(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)

    repo.increment_binding_poll_count(binding.binding_id)
    repo.increment_binding_poll_count(binding.binding_id)
    repo.increment_binding_sent_count(binding.binding_id, datetime.now(timezone.utc))

    fetched = repo.get_binding(binding.binding_id)
    assert fetched.total_polls == 2
    assert fetched.total_sent == 1
    assert fetched.last_send_utc is not None


def test_set_binding_enabled(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)

    repo.set_binding_enabled(binding.binding_id, False)
    assert repo.get_binding(binding.binding_id).is_enabled is False


def test_message_dedup_by_content_hash(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)
    message_hash = compute_content_hash("hello world")

    assert repo.was_message_already_handled(binding.binding_id, None, message_hash) is False

    message = WhatsAppFetchRelayMessageEntity(
        binding_id=binding.binding_id,
        message_text="hello world",
        content_hash=message_hash,
        state=WhatsAppFetchRelayMessageState.SENT,
    )
    repo.insert_message(message)

    assert repo.was_message_already_handled(binding.binding_id, None, message_hash) is True


def test_has_in_flight_message_true_for_pending_and_sending(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)
    message_hash = compute_content_hash("in flight")

    message = WhatsAppFetchRelayMessageEntity(
        binding_id=binding.binding_id, message_text="in flight", content_hash=message_hash, state=WhatsAppFetchRelayMessageState.PENDING
    )
    repo.insert_message(message)

    assert repo.has_in_flight_message(binding.binding_id, None, message_hash) is True

    repo.update_message(replace(message, state=WhatsAppFetchRelayMessageState.SENT))
    assert repo.has_in_flight_message(binding.binding_id, None, message_hash) is False


def test_get_retryable_messages_respects_next_retry_utc(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)

    future_retry = WhatsAppFetchRelayMessageEntity(
        binding_id=binding.binding_id,
        message_text="later",
        content_hash="h1",
        state=WhatsAppFetchRelayMessageState.RETRYING,
        next_retry_utc=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    ready_retry = WhatsAppFetchRelayMessageEntity(
        binding_id=binding.binding_id,
        message_text="now",
        content_hash="h2",
        state=WhatsAppFetchRelayMessageState.RETRYING,
        next_retry_utc=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    exhausted = WhatsAppFetchRelayMessageEntity(
        binding_id=binding.binding_id,
        message_text="exhausted",
        content_hash="h3",
        state=WhatsAppFetchRelayMessageState.RETRYING,
        attempt_count=3,
    )
    repo.insert_message(future_retry)
    repo.insert_message(ready_retry)
    repo.insert_message(exhausted)

    retryable = repo.get_retryable_messages()

    assert {m.message_text for m in retryable} == {"now"}


def test_get_recent_messages_orders_newest_first(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Infosys", fetch_url="http://x")
    repo.upsert_binding(binding)

    older = WhatsAppFetchRelayMessageEntity(
        binding_id=binding.binding_id, message_text="older", content_hash="h1", fetch_utc=datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    newer = WhatsAppFetchRelayMessageEntity(binding_id=binding.binding_id, message_text="newer", content_hash="h2")
    repo.insert_message(older)
    repo.insert_message(newer)

    recent = repo.get_recent_messages()
    assert recent[0].message_text == "newer"
    assert recent[1].message_text == "older"


def test_compute_content_hash_is_stable_and_trims_whitespace():
    assert compute_content_hash("hello") == compute_content_hash("hello")
    assert compute_content_hash("hello") == compute_content_hash("  hello  ")
    assert compute_content_hash("hello") != compute_content_hash("world")


def test_chat_memory_appends_and_reads_oldest_first(tmp_path):
    repo = _make_repo(tmp_path)
    repo.append_chat_memory("Manohar", "them", "Manohar", "hi, I'm Dan")
    repo.append_chat_memory("Manohar", "me", "", "hey Dan!")
    repo.append_chat_memory("OtherChat", "them", "", "unrelated")   # a different chat's memory

    assert repo.get_chat_memory("Manohar") == [
        ("them", "Manohar", "hi, I'm Dan"),
        ("me", "", "hey Dan!"),
    ]
    assert repo.get_chat_memory("OtherChat") == [("them", "", "unrelated")]


def test_chat_memory_keeps_only_the_newest_k(tmp_path):
    repo = _make_repo(tmp_path)
    for i in range(30):
        repo.append_chat_memory("Manohar", "them", "", f"msg {i}", keep=5)

    remembered = repo.get_chat_memory("Manohar")
    assert [t for _, _, t in remembered] == ["msg 25", "msg 26", "msg 27", "msg 28", "msg 29"]


def test_chat_memory_ignores_blank_input(tmp_path):
    repo = _make_repo(tmp_path)
    repo.append_chat_memory("", "them", "", "no group")
    repo.append_chat_memory("Manohar", "them", "", "   ")
    assert repo.get_chat_memory("Manohar") == []


def test_deleting_a_binding_forgets_its_chats_memory(tmp_path):
    repo = _make_repo(tmp_path)
    binding = WhatsAppFetchBindingEntity(group_name="Manohar", fetch_url="http://x")
    repo.upsert_binding(binding)
    repo.append_chat_memory("Manohar", "them", "", "remember me")
    repo.append_chat_memory("Sharon", "them", "", "keep me")   # no binding deleted for this chat

    repo.delete_binding(binding.binding_id)

    assert repo.get_chat_memory("Manohar") == []
    assert repo.get_chat_memory("Sharon") == [("them", "", "keep me")]
