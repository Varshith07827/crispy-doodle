"""Fast, cross-platform tests for WhatsAppGroupSender.send_to_group_async's
control flow — prefer the Send BUTTON, fall back to Enter, verify the box
cleared, retry without blindly re-typing, and report failure honestly.

These stub out the UIA sync helpers (the real ones need Windows + a running
WhatsApp), so they exercise the orchestration, not the accessibility calls.
The end-to-end delivery is verified live in test_whatsapp_group_sender.py.
"""

import asyncio

import pytest

from winspark.connectors import whatsapp_group_sender as gs
from winspark.connectors.fetch_webhook_models import WhatsAppGroupSendResult  # noqa: F401 (imported for parity)
from winspark.connectors.whatsapp_group_sender import WhatsAppGroupSender


class _FakeSta:
    """Runs the submitted callables inline on the calling thread."""

    def __init__(self):
        self.action_lock = asyncio.Lock()  # matches the real STA manager's send-serialising lock

    async def invoke_async(self, fn):
        return fn()


class _FakeRow:
    raw_text = "Manohar 12:39 pm hello"


class _FakeConnector:
    async def find_window_async(self):
        return 4242

    async def get_active_conversation_name_async(self, handle):
        return "Manohar"


def _sender(monkeypatch, *, box_after_send):
    """Wire a sender whose chat resolves and opens, and whose compose box
    reports `box_after_send.pop(0)` empty/non-empty after each send attempt."""
    sender = WhatsAppGroupSender(_FakeConnector(), _FakeSta())

    async def fake_resolve(group_name):
        return 4242, _FakeRow()

    sender.resolve_chat_row_async = fake_resolve
    monkeypatch.setattr(gs, "_open_chat_sync", lambda *a, **k: True)

    typed = {"text": ""}
    monkeypatch.setattr(gs, "_set_compose_text_sync",
                        lambda handle, text: typed.__setitem__("text", text) or True)
    monkeypatch.setattr(gs, "_read_compose_text_sync", lambda handle: typed["text"])

    calls = {"button": 0, "enter": 0}

    def is_empty(handle):
        return box_after_send.pop(0) if box_after_send else True

    monkeypatch.setattr(gs, "_compose_is_empty_sync", is_empty)
    return sender, calls, typed


def test_send_prefers_the_button_and_does_not_press_enter(monkeypatch):
    sender, calls, _ = _sender(monkeypatch, box_after_send=[True])   # clears on first check
    monkeypatch.setattr(gs, "_invoke_send_button_sync",
                        lambda h: calls.__setitem__("button", calls["button"] + 1) or True)
    monkeypatch.setattr(gs, "_send_compose_sync",
                        lambda h: calls.__setitem__("enter", calls["enter"] + 1) or True)

    res = asyncio.run(sender.send_to_group_async("Manohar", "hi"))

    assert res.success and res.verified
    assert calls == {"button": 1, "enter": 0}   # button used, Enter never touched


def test_send_falls_back_to_enter_when_no_button(monkeypatch):
    sender, calls, _ = _sender(monkeypatch, box_after_send=[True])
    monkeypatch.setattr(gs, "_invoke_send_button_sync",
                        lambda h: calls.__setitem__("button", calls["button"] + 1) or False)  # no button
    monkeypatch.setattr(gs, "_send_compose_sync",
                        lambda h: calls.__setitem__("enter", calls["enter"] + 1) or True)

    res = asyncio.run(sender.send_to_group_async("Manohar", "hi"))

    assert res.success
    assert calls == {"button": 1, "enter": 1}   # tried the button, then Enter


def test_send_retries_without_retyping_when_text_survived(monkeypatch):
    # First send doesn't clear the box; second one does. The text is still
    # present between attempts, so it must NOT be re-typed.
    sender, calls, typed = _sender(monkeypatch, box_after_send=[False] * 10 + [True])
    typed["text"] = "hi"   # the box already holds the right text
    set_calls = {"n": 0}
    orig_set = gs._set_compose_text_sync

    def counting_set(handle, text):
        set_calls["n"] += 1
        return orig_set(handle, text)

    monkeypatch.setattr(gs, "_set_compose_text_sync", counting_set)
    monkeypatch.setattr(gs, "_invoke_send_button_sync",
                        lambda h: calls.__setitem__("button", calls["button"] + 1) or True)
    monkeypatch.setattr(gs, "_send_compose_sync", lambda h: True)

    res = asyncio.run(sender.send_to_group_async("Manohar", "hi"))

    assert res.success
    assert calls["button"] == 2          # sent twice (first didn't take)
    assert set_calls["n"] == 1           # only the initial type — never RE-typed on retry


def test_a_failed_type_does_not_leave_text_in_the_chat(monkeypatch):
    """Seen live: a long answer failed to verify, this path returned straight
    out, and the half-written message sat in the user's compose box while every
    retry typed over it. Only the send-loop's exit used to clean up."""
    sender, _calls, _typed = _sender(monkeypatch, box_after_send=[True])
    cleared: list[str] = []

    def failing_set(handle, text):
        cleared.append(text)
        return False              # the type never verifies

    monkeypatch.setattr(gs, "_set_compose_text_sync", failing_set)
    monkeypatch.setattr(gs, "_invoke_send_button_sync", lambda h: True)
    monkeypatch.setattr(gs, "_send_compose_sync", lambda h: True)

    res = asyncio.run(sender.send_to_group_async("Manohar", "a long answer"))

    assert not res.success
    assert cleared[-1] == ""      # the box was emptied before giving up


def test_retry_does_not_retype_a_multiline_message_already_in_the_box(monkeypatch):
    """The compose box doesn't read a typed newline back as "\\n", so comparing
    exactly said "the text went missing" every time and retyped the whole
    answer — 21 seconds per attempt for a long one, which is what the user saw
    as "it keeps rewriting the message"."""
    message = "line one\nline two\nline three"
    sender, calls, typed = _sender(monkeypatch, box_after_send=[False] * 10 + [True])
    # What the box reads back: same text, newlines rendered as spaces.
    typed["text"] = message.replace("\n", " ")
    set_calls = {"n": 0}

    monkeypatch.setattr(gs, "_set_compose_text_sync",
                        lambda h, t: set_calls.__setitem__("n", set_calls["n"] + 1) or True)
    monkeypatch.setattr(gs, "_invoke_send_button_sync", lambda h: True)
    monkeypatch.setattr(gs, "_send_compose_sync", lambda h: True)

    res = asyncio.run(sender.send_to_group_async("Manohar", message))

    assert res.success
    assert set_calls["n"] == 1    # typed once; the retry just pressed Send again


def test_send_reports_failure_when_the_box_never_clears(monkeypatch):
    sender, calls, _ = _sender(monkeypatch, box_after_send=[False] * 60)   # never clears
    monkeypatch.setattr(gs, "_invoke_send_button_sync", lambda h: True)
    monkeypatch.setattr(gs, "_send_compose_sync", lambda h: True)
    cleared = {"n": 0}
    monkeypatch.setattr(gs, "_set_compose_text_sync",
                        lambda h, t: cleared.__setitem__("n", cleared["n"] + 1) or True)

    res = asyncio.run(sender.send_to_group_async("Manohar", "hi"))

    assert not res.success                       # honest failure, not a soft success
    assert "not sent" in res.failure_reason
    assert cleared["n"] >= 1                      # left nothing half-typed in the chat
