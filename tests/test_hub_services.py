"""The two hub flows, which are deliberately independent of each other:

  capture  — save the open chat's messages to MongoDB as they arrive
  spool    — poll a chat's linked webhook and send what it returns

Both are driven by an explicit `tick()` so the tests exercise the logic without
timers, WhatsApp, or a Mongo server.
"""

import pytest

from winspark.hub.capture_service import CaptureService
from winspark.hub.settings_files import HubSettings
from winspark.hub.spool_service import SpoolService


class _Msg:
    def __init__(self, text, sender="V", is_incoming=True, time_text="1:54 pm", media_kind=""):
        self.text, self.sender, self.is_incoming = text, sender, is_incoming
        self.time_text, self.media_kind, self.media_note = time_text, media_kind, ""


class _FakeStore:
    """Stands in for MessageHubStore, with its idempotent-upsert behaviour."""

    def __init__(self, fail=False):
        self.docs = {}
        self.fail = fail
        self.last_error = ""

    def save(self, message):
        if self.fail:
            self.last_error = "server went away"
            return False
        if not (message.chat or "").strip() or not (message.text or "").strip():
            return False
        self.docs[message.fingerprint] = message
        self.last_error = ""
        return True

    def save_many(self, messages):
        stored = failed = 0
        for m in messages:
            if self.save(m):
                stored += 1
            else:
                failed += 1
        return stored, failed


@pytest.fixture
def settings(tmp_path):
    return HubSettings(tmp_path)


def _capture(settings, chat, messages, store=None):
    store = store if store is not None else _FakeStore()
    service = CaptureService(settings, lambda: (chat, messages), lambda: store)
    return service, store


# --- capture ------------------------------------------------------------------

def test_an_enabled_chat_is_saved(settings):
    settings.set_capture("Varshith", True)
    service, store = _capture(settings, "Varshith", [_Msg("you around?")])

    assert service.tick() == 1
    assert len(store.docs) == 1


def test_nothing_is_saved_for_a_chat_that_was_not_opted_in(settings):
    """Reading is unavoidable — we can't know which chat is open without
    looking — but an un-opted-in chat must never be stored."""
    settings.set_capture("Varshith", True)
    service, store = _capture(settings, "Nagen US", [_Msg("private")])

    assert service.tick() == 0
    assert store.docs == {}
    assert "isn't set to save" in service.status_line()


def test_nothing_is_saved_when_no_chat_is_enabled(settings):
    service, store = _capture(settings, "Varshith", [_Msg("hi")])

    assert service.tick() == 0
    assert store.docs == {}
    assert "No chats are set to save" in service.status_line()


def test_turning_capture_off_stops_saving(settings):
    settings.set_capture("Varshith", True)
    service, store = _capture(settings, "Varshith", [_Msg("one")])
    service.tick()

    settings.set_capture("Varshith", False)
    assert service.tick() == 0
    assert len(store.docs) == 1          # the first one only


def test_repeated_ticks_do_not_duplicate_the_visible_messages(settings):
    """The reader re-offers the whole screen every few seconds; the store's
    fingerprint upsert is what makes that free."""
    settings.set_capture("Varshith", True)
    service, store = _capture(settings, "Varshith", [_Msg("one"), _Msg("two")])

    for _ in range(5):
        service.tick()

    assert len(store.docs) == 2


def test_new_messages_are_picked_up_as_they_appear(settings):
    settings.set_capture("Varshith", True)
    screen = [_Msg("one", time_text="1:00 pm")]
    service, store = _capture(settings, "Varshith", screen)
    service.tick()

    screen.append(_Msg("two", time_text="1:01 pm"))
    service.tick()

    assert len(store.docs) == 2


def test_both_sides_of_the_conversation_are_saved(settings):
    settings.set_capture("Varshith", True)
    service, store = _capture(settings, "Varshith", [
        _Msg("you around?", is_incoming=True),
        _Msg("yes", sender="", is_incoming=False, time_text="1:55 pm"),
    ])
    service.tick()

    assert {m.direction for m in store.docs.values()} == {"in", "out"}


def test_a_lost_message_is_reported_because_there_is_no_local_copy(settings):
    settings.set_capture("Varshith", True)
    service, store = _capture(settings, "Varshith", [_Msg("hi")], store=_FakeStore(fail=True))

    assert service.tick() == 0
    assert "server went away" in service.status_line()


def test_capture_survives_mongodb_being_unavailable(settings):
    settings.set_capture("Varshith", True)
    service = CaptureService(settings, lambda: ("Varshith", [_Msg("hi")]), lambda: None)

    assert service.tick() == 0
    assert "MongoDB isn't connected" in service.status_line()


def test_a_failing_read_does_not_kill_the_service(settings):
    settings.set_capture("Varshith", True)

    def boom():
        raise RuntimeError("WhatsApp went away")

    service = CaptureService(settings, boom, lambda: _FakeStore())
    assert service.tick() == 0                       # must not raise
    assert "WhatsApp went away" in service.status_line()


# --- spool --------------------------------------------------------------------

def _spool(settings, responses, send_result=(True, "")):
    sent = []

    def fetch(url):
        return responses.pop(0) if responses else ""

    def send(chat, text):
        sent.append((chat, text))
        return send_result

    return SpoolService(settings, fetch, send), sent


def test_a_linked_chat_sends_what_the_webhook_returns(settings):
    settings.set_send_link("Varshith", "https://hook.example", enabled=True)
    service, sent = _spool(settings, ["hello from the webhook"])

    assert service.tick(now=100.0) == 1
    assert sent == [("Varshith", "hello from the webhook")]


def test_an_empty_response_sends_nothing(settings):
    settings.set_send_link("Varshith", "https://hook.example", enabled=True)
    service, sent = _spool(settings, ["   "])

    assert service.tick(now=100.0) == 0
    assert sent == []


def test_a_disabled_link_is_not_polled(settings):
    settings.set_send_link("Varshith", "https://hook.example", enabled=False)
    service, sent = _spool(settings, ["something"])

    assert service.tick(now=100.0) == 0
    assert sent == []


def test_the_interval_is_respected(settings):
    settings.set_send_link("Varshith", "https://hook.example", enabled=True, interval_seconds=3)
    service, sent = _spool(settings, ["one", "two", "three"])

    service.tick(now=100.0)          # polls
    service.tick(now=101.0)          # too soon
    service.tick(now=102.9)          # still too soon
    assert len(sent) == 1

    service.tick(now=103.1)          # due again
    assert len(sent) == 2


def test_the_same_text_twice_is_sent_twice(settings):
    """The webhook is a queue, not a state — two "ping"s are two things to send."""
    settings.set_send_link("Varshith", "https://hook.example", enabled=True)
    service, sent = _spool(settings, ["ping", "ping"])

    service.tick(now=100.0)
    service.tick(now=200.0)

    assert sent == [("Varshith", "ping"), ("Varshith", "ping")]


def test_a_failed_send_is_reported(settings):
    settings.set_send_link("Varshith", "https://hook.example", enabled=True)
    service, _sent = _spool(settings, ["hi"], send_result=(False, "WhatsApp isn't running"))

    service.tick(now=100.0)
    assert "WhatsApp isn't running" in service.status_line("Varshith")


def test_one_broken_link_does_not_stop_the_others(settings):
    settings.set_send_link("Bad", "https://bad.example", enabled=True)
    settings.set_send_link("Good", "https://good.example", enabled=True)
    sent = []

    def fetch(url):
        if "bad" in url:
            raise RuntimeError("connection refused")
        return "delivered"

    service = SpoolService(settings, fetch, lambda c, t: (sent.append((c, t)), (True, ""))[1])
    service.tick(now=100.0)

    assert sent == [("Good", "delivered")]
    assert "connection refused" in service.status_line("Bad")


def test_links_are_independent_of_capture(settings):
    """Linking a webhook must not start saving that chat to MongoDB."""
    settings.set_send_link("Varshith", "https://hook.example", enabled=True)
    assert settings.is_capturing("Varshith") is False
    assert settings.capturing_chats() == ()


def test_status_line_before_anything_is_linked(settings):
    service, _sent = _spool(settings, [])
    assert "Not linked" in service.status_line("Varshith")
