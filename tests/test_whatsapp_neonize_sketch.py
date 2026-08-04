"""The parts of the protocol-backed connector that can be tested without a
paired WhatsApp session: mapping WhatsApp's message types onto winSpark's
MEDIA_* vocabulary, and the recent-message buffer that stands in for the
history whatsmeow cannot fetch.

Sending, downloading and pairing all need a real session and are deliberately
NOT faked here — a green test against a mock of a protocol I can't run would be
worse than no test, because it would look like verification.
"""

import pytest

from winspark.connectors.whatsapp import (
    MEDIA_DOCUMENT,
    MEDIA_GIF,
    MEDIA_PHOTO,
    MEDIA_STICKER,
    MEDIA_VIDEO,
    MEDIA_VOICE,
    WhatsAppMessage,
    media_placeholder,
)
from winspark.connectors.whatsapp_neonize import _media_of, _plain_text


class _Field:
    """Stands in for one protobuf sub-message (imageMessage, audioMessage, ...)."""

    def __init__(self, **kw):
        self.caption = kw.pop("caption", "")
        self.fileName = kw.pop("fileName", "")  # noqa: N815 - protobuf spelling
        self.seconds = kw.pop("seconds", 0)
        self.gifPlayback = kw.pop("gifPlayback", False)  # noqa: N815
        self.mimetype = kw.pop("mimetype", "")


class _Msg:
    """A protobuf Message: exactly one media field set, or plain text."""

    def __init__(self, present=None, conversation="", extended_text="", **fields):
        self._present = present
        self.conversation = conversation
        self.extendedTextMessage = _Field()  # noqa: N815
        self.extendedTextMessage.text = extended_text
        for name in ("imageMessage", "audioMessage", "videoMessage",
                     "documentMessage", "stickerMessage"):
            setattr(self, name, _Field(**(fields if name == present else {})))

    def HasField(self, name):  # noqa: N802 - protobuf API
        return name == self._present


@pytest.mark.parametrize("present, fields, kind, note", [
    ("imageMessage", {"caption": "look at this"}, MEDIA_PHOTO, "look at this"),
    ("imageMessage", {}, MEDIA_PHOTO, ""),
    ("audioMessage", {"seconds": 12}, MEDIA_VOICE, "0:12"),
    ("audioMessage", {"seconds": 75}, MEDIA_VOICE, "1:15"),
    ("videoMessage", {"caption": "clip"}, MEDIA_VIDEO, "clip"),
    ("documentMessage", {"fileName": "report.pdf"}, MEDIA_DOCUMENT, "report.pdf"),
    ("stickerMessage", {}, MEDIA_STICKER, ""),
])
def test_whatsapp_media_types_map_onto_winspark_kinds(present, fields, kind, note):
    got_kind, got_note, payload = _media_of(_Msg(present=present, **fields))
    assert (got_kind, got_note) == (kind, note)
    assert payload is not None          # the sub-message carrying the bytes


def test_a_gif_is_not_reported_as_a_video():
    """WhatsApp sends a GIF as a videoMessage with gifPlayback set, not its own
    type — checking video first would silently mislabel every GIF."""
    kind, _note, _payload = _media_of(_Msg(present="videoMessage", gifPlayback=True))
    assert kind == MEDIA_GIF


def test_plain_text_reports_no_media():
    assert _media_of(_Msg(conversation="just talking")) == ("", "", None)


@pytest.mark.parametrize("msg, expected", [
    (_Msg(conversation="hello"), "hello"),
    (_Msg(extended_text="hello with a link"), "hello with a link"),
    (_Msg(), ""),
])
def test_text_is_read_from_either_shape_whatsapp_uses(msg, expected):
    assert _plain_text(msg) == expected


def test_media_messages_render_the_same_text_as_the_ui_reader():
    """Both connectors must produce the same placeholder, or a chat's memory
    would read differently depending on which one captured it."""
    kind, note, _ = _media_of(_Msg(present="audioMessage", seconds=12))
    assert media_placeholder(kind, note) == "[Voice note · 0:12]"

    kind, note, _ = _media_of(_Msg(present="documentMessage", fileName="report.pdf"))
    assert media_placeholder(kind, note) == "[Document: report.pdf]"


def test_the_message_type_carries_a_real_file_path():
    """The whole point of this connector: a path to actual bytes, where the
    UI-Automation reader can only ever leave this empty."""
    message = WhatsAppMessage(sender="V", text="[Photo] hi", is_incoming=True,
                              media_kind=MEDIA_PHOTO, media_note="hi",
                              media_path=r"C:\media\abc_photo.jpg")
    assert message.media_path.endswith("abc_photo.jpg")
    # Default stays empty, so the existing reader and its tests are unaffected.
    assert WhatsAppMessage(sender="V", text="hi", is_incoming=True).media_path == ""


# --- things the real package proved wrong about the first draft ---------------
#
# Each of these was a genuine bug found by introspecting the installed neonize,
# not a hypothetical. They're pinned so a later edit can't quietly restore them.

def test_a_jid_is_rendered_as_user_at_server_not_protobuf_debug_text():
    """str() on a JID gives 'User: "9199..."\\nServer: "..."\\n' — using it as a
    dict key looked fine and matched nothing."""
    from winspark.connectors.whatsapp_neonize import _jid_str

    class _Jid:
        User, Server = "919999999999", "s.whatsapp.net"

    assert _jid_str(_Jid()) == "919999999999@s.whatsapp.net"
    assert "User:" not in _jid_str(_Jid())

    class _Empty:
        User, Server = "", ""

    assert _jid_str(_Empty()) == ""
    assert _jid_str(None) == ""


def test_the_message_timestamp_is_a_unix_int_not_a_datetime():
    """MessageInfo.Timestamp is TYPE_INT64. The first draft called .strftime()
    on it, guarded by hasattr — so time_text was silently always empty."""
    from winspark.connectors.whatsapp_neonize import _time_label

    # 2026-08-03 13:02 local, whatever the runner's zone is.
    import datetime as _dt
    stamp = int(_dt.datetime(2026, 8, 3, 13, 2).timestamp())
    assert _time_label(stamp) == "1:02 pm"

    assert _time_label(0) == ""            # absent timestamp, not a crash
    assert _time_label(-(10 ** 18)) == ""  # nonsense, not a traceback


def test_the_hour_is_unpadded_without_the_flag_that_breaks_on_windows():
    """The codebase already learned this in _current_datetime_line: %-I raises
    ValueError on Windows, the only platform winSpark runs on. The first draft
    used it, so every incoming message would have crashed the handler."""
    import datetime as _dt

    from winspark.connectors.whatsapp_neonize import _time_label

    with pytest.raises(ValueError):
        _dt.datetime.now().strftime("%-I:%M %p")     # what the draft did

    morning = int(_dt.datetime(2026, 8, 3, 9, 5).timestamp())
    assert _time_label(morning) == "9:05 am"         # unpadded, and it works


def test_a_phone_number_can_be_addressed_without_a_contact_list():
    """neonize has no list-my-contacts call, so someone who has never messaged
    you is unreachable by name. A number has to work as a fallback."""
    sender = _sender_with_buffer({})
    assert sender._jid_for("919999999999") == "919999999999@s.whatsapp.net"
    assert sender._jid_for("+91 99999-99999") == "919999999999@s.whatsapp.net"
    assert sender._jid_for("Some Person") is None    # a name still can't be guessed
    assert sender._jid_for("") is None


def test_a_one_to_one_chat_is_learned_from_whoever_messages():
    """The only way this connector ever knows a personal chat by name."""
    sender = _sender_with_buffer({})
    assert sender._jid_for("Ravi") is None

    sender._remember_sender("919999999999@s.whatsapp.net", "Ravi", is_group=False)
    assert sender._jid_for("Ravi") == "919999999999@s.whatsapp.net"

    # A group's name comes from the directory, not from its senders — otherwise
    # a participant's push name would start resolving to the whole group.
    sender._remember_sender("Family@g.us", "Asha", is_group=True)
    assert sender._jid_for("Asha") is None


# --- the recent-message buffer ------------------------------------------------

def _sender_with_buffer(messages_by_chat):
    """A NeonizeGroupSender with its buffer pre-filled, bypassing the network."""
    from collections import deque

    from winspark.connectors.whatsapp_neonize import RECENT_PER_CHAT, NeonizeGroupSender

    sender = NeonizeGroupSender("test", "test.db", "media")
    for jid, messages in messages_by_chat.items():
        sender._recent[jid] = deque(messages, maxlen=RECENT_PER_CHAT)
        sender._directory[jid.split("@")[0]] = jid
    return sender


def _incoming(text):
    return WhatsAppMessage(sender="Ravi", text=text, is_incoming=True)


@pytest.mark.asyncio
async def test_recent_read_returns_oldest_first_and_respects_the_limit():
    sender = _sender_with_buffer({
        "Family@g.us": [_incoming("one"), _incoming("two"), _incoming("three")],
    })

    got = await sender.read_recent_incoming_async("Family", limit=2)
    assert [m.text for m in got] == ["two", "three"]     # newest two, oldest first

    last = await sender.read_last_incoming_async("Family")
    assert last.text == "three"
    assert await sender.read_last_incoming_message_async("Family") == "three"


@pytest.mark.asyncio
async def test_an_unknown_chat_reads_empty_rather_than_raising():
    sender = _sender_with_buffer({})
    assert await sender.read_recent_incoming_async("Nobody") == []
    assert await sender.read_last_incoming_async("Nobody") is None
    assert await sender.can_resolve_chat_async("Nobody") is False


@pytest.mark.asyncio
async def test_a_cold_session_has_no_history_to_offer():
    """whatsmeow cannot ask WhatsApp for past messages, so a known chat with an
    empty buffer really is empty — the one place this connector is WORSE than
    reading the screen, and it must fail quietly rather than look broken."""
    sender = _sender_with_buffer({"Family@g.us": []})

    assert await sender.can_resolve_chat_async("Family") is True    # chat is known
    assert await sender.read_recent_incoming_async("Family") == []  # but nothing yet


@pytest.mark.asyncio
async def test_sending_to_an_unknown_chat_fails_without_a_client():
    """Guards the resolve step: it must not reach for the (absent) client."""
    sender = _sender_with_buffer({})
    result = await sender.send_to_group_async("Nobody", "hi")
    assert not result.success
    assert "not found" in result.failure_reason
