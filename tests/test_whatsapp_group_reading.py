"""Tests for reading messages in GROUP chats — the fault that broke a live
demo: in a group, only OUR bubbles carry the "You:" label group, so the
labeled read saw one side of the conversation and "reply to the newest
incoming message" concluded there was never anything to reply to.

The Windows-UIA tree walking is stubbed; these test the selection logic in
_read_recent_messages_sync (which read path wins) — the part that decided the
demo's fate."""

import pytest

from winspark.connectors import whatsapp
from winspark.connectors.whatsapp import WhatsAppMessage


def _msg(text, incoming, sender=None):
    return WhatsAppMessage(
        sender=sender if sender is not None else ("" if incoming else "You"),
        text=text,
        is_incoming=incoming,
    )


@pytest.fixture
def stub_tree(monkeypatch):
    """Make _read_recent_messages_sync run against injected path results."""
    monkeypatch.setattr(whatsapp, "_require_win32", lambda: None)

    class _FakeAuto:
        @staticmethod
        def ControlFromHandle(_handle):
            return object()  # non-None sentinel; paths below are stubbed

    monkeypatch.setattr(whatsapp, "auto", _FakeAuto)

    def set_paths(labeled, bubbles):
        monkeypatch.setattr(whatsapp, "_read_labeled_messages", lambda root: list(labeled))
        monkeypatch.setattr(whatsapp, "_read_bubble_messages", lambda root: list(bubbles))

    return set_paths


def test_group_shape_uses_bubbles_when_labels_only_show_our_side(stub_tree):
    # The live-observed group state: labels see ONLY our messages, bubbles see
    # the whole conversation including members' messages.
    stub_tree(
        labeled=[_msg("I'll join later", incoming=False)],
        bubbles=[
            _msg("join me in our meet", incoming=True),
            _msg("I'll join later", incoming=False),
            _msg("Rara babu", incoming=True),
        ],
    )
    messages = whatsapp._read_recent_messages_sync(1, limit=10)
    assert len(messages) == 3
    assert messages[-1].text == "Rara babu" and messages[-1].is_incoming is True


def test_one_to_one_with_incoming_keeps_the_labeled_read(stub_tree):
    # Healthy 1:1: labels see both sides — must stay authoritative (senders!).
    stub_tree(
        labeled=[_msg("dinner at 8?", True, sender="Family"), _msg("sounds good", False)],
        bubbles=[_msg("dinner at 8?", True), _msg("sounds good", False), _msg("junk row", True)],
    )
    messages = whatsapp._read_recent_messages_sync(1, limit=10)
    assert [m.text for m in messages] == ["dinner at 8?", "sounds good"]
    assert messages[0].sender == "Family"


def test_all_outgoing_one_to_one_without_richer_bubbles_stays_labeled(stub_tree):
    # We messaged someone who never replied: all-outgoing is legitimate, and
    # the bubble read has nothing more — don't switch for no reason.
    stub_tree(
        labeled=[_msg("hello?", False)],
        bubbles=[_msg("hello?", False)],
    )
    messages = whatsapp._read_recent_messages_sync(1, limit=10)
    assert len(messages) == 1 and messages[0].is_incoming is False


def test_empty_labels_fall_back_to_bubbles(stub_tree):
    stub_tree(labeled=[], bubbles=[_msg("hi", True)])
    messages = whatsapp._read_recent_messages_sync(1, limit=10)
    assert len(messages) == 1 and messages[0].is_incoming is True


def test_limit_returns_the_newest_tail(stub_tree):
    stub_tree(labeled=[], bubbles=[_msg(f"m{i}", True) for i in range(10)])
    messages = whatsapp._read_recent_messages_sync(1, limit=3)
    assert [m.text for m in messages] == ["m7", "m8", "m9"]
