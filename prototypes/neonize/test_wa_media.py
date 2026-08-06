"""Offline tests for the media classify/name/save path.

Every test here builds a real WhatsApp protobuf by hand and uses a stub client,
so the whole pipeline is verified WITHOUT pairing an account or touching the
network. Only the single `client.download_any()` call is stubbed — everything
that decides what a message contains, what to call the file and what to record
about it runs for real.

    python -m pytest test_wa_media.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import wa_media


def _image(mimetype="image/jpeg", caption="", **kw) -> Message:
    msg = Message()
    msg.imageMessage.mimetype = mimetype
    if caption:
        msg.imageMessage.caption = caption
    for k, v in kw.items():
        setattr(msg.imageMessage, k, v)
    return msg


class _StubClient:
    """Stands in for the live client: returns fixed bytes for any download."""

    def __init__(self, payload: bytes = b"\xff\xd8\xff binary payload"):
        self.payload = payload
        self.calls = 0

    def download_any(self, message):
        self.calls += 1
        return self.payload


# --- what kind of attachment is this? ----------------------------------------

def test_photo_is_classified_with_its_caption_and_dimensions():
    msg = _image(caption="on the beach", width=1920, height=1080, fileLength=482913)
    media = wa_media.classify(msg)

    assert media.kind == "image"
    assert media.caption == "on the beach"
    assert (media.width, media.height) == (1920, 1080)
    assert media.file_length == 482913
    assert media.extension == ".jpg"


def test_a_voice_note_is_told_apart_from_a_music_file():
    """Same protobuf, PTT flag apart — but a recorded voice note and a shared
    song are completely different things to the person reading the chat, and
    winSpark's chat memory already distinguishes them."""
    voice = Message()
    voice.audioMessage.mimetype = "audio/ogg; codecs=opus"
    voice.audioMessage.PTT = True
    voice.audioMessage.seconds = 12

    music = Message()
    music.audioMessage.mimetype = "audio/mpeg"
    music.audioMessage.PTT = False

    assert wa_media.classify(voice).kind == "voice"
    assert wa_media.classify(voice).seconds == 12
    assert wa_media.classify(voice).extension == ".ogg"   # codecs= param ignored
    assert wa_media.classify(music).kind == "audio"
    assert wa_media.classify(music).extension == ".mp3"


def test_document_keeps_its_original_name_and_page_count():
    msg = Message()
    msg.documentMessage.mimetype = "application/pdf"
    msg.documentMessage.fileName = "Q3 report.pdf"
    msg.documentMessage.pageCount = 14
    media = wa_media.classify(msg)

    assert media.kind == "document"
    assert media.file_name == "Q3 report.pdf"
    assert media.page_count == 14
    assert media.extension == ".pdf"


def test_video_and_sticker_are_classified():
    video = Message()
    video.videoMessage.mimetype = "video/mp4"
    video.videoMessage.seconds = 30
    assert wa_media.classify(video).kind == "video"

    sticker = Message()
    sticker.stickerMessage.mimetype = "image/webp"
    media = wa_media.classify(sticker)
    assert media.kind == "sticker"
    assert media.extension == ".webp"


def test_a_text_only_message_has_no_media():
    msg = Message()
    msg.conversation = "just talking"
    assert wa_media.classify(msg) is None


def test_a_wrapped_document_is_still_found():
    """documentWithCaptionMessage nests the real document one level down.
    Without unwrapping, a captioned PDF looks like a message with no media."""
    msg = Message()
    inner = msg.documentWithCaptionMessage.message
    inner.documentMessage.mimetype = "application/pdf"
    inner.documentMessage.fileName = "contract.pdf"

    media = wa_media.classify(msg)
    assert media is not None
    assert media.kind == "document"
    assert media.file_name == "contract.pdf"


def test_extension_falls_back_to_the_original_filename():
    """An unknown mimetype must not produce a .bin the OS can't open."""
    msg = Message()
    msg.documentMessage.mimetype = "application/x-something-odd"
    msg.documentMessage.fileName = "archive.zip"
    assert wa_media.classify(msg).extension == ".zip"


# --- naming ------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Nagen US", "Nagen_US"),
    ("Family 💖 Group", "Family_Group"),
    ("a/b:c*d?e", "a_b_c_d_e"),
    ("", "unknown"),
    ("   ", "unknown"),
    ("...", "unknown"),
])
def test_names_are_made_filesystem_safe(raw, expected):
    """Chat names are full of emoji and punctuation Windows rejects outright."""
    assert wa_media.safe_slug(raw) == expected


def test_the_saved_path_sorts_by_time_and_says_what_it_is(tmp_path):
    media = wa_media.classify(_image())
    plan = wa_media.plan_save(
        media, tmp_path, chat="Nagen US", sender="919999",
        message_id="ABCDEF123456789", when=datetime(2026, 8, 3, 14, 5, 9, tzinfo=timezone.utc))

    assert plan.path.parent.name == "image"          # grouped by kind
    assert plan.path.name.startswith("20260803-140509-Nagen_US-image-")
    assert plan.path.suffix == ".jpg"
    assert plan.metadata_path.suffix == ".json"


def test_the_same_file_sent_twice_does_not_overwrite(tmp_path):
    media = wa_media.classify(_image())
    when = datetime(2026, 8, 3, 14, 5, 9, tzinfo=timezone.utc)
    first = wa_media.plan_save(media, tmp_path, chat="X", message_id="AAAA1111", when=when)
    second = wa_media.plan_save(media, tmp_path, chat="X", message_id="BBBB2222", when=when)
    assert first.path != second.path


# --- writing -----------------------------------------------------------------

def test_bytes_and_sidecar_are_both_written(tmp_path):
    media = wa_media.classify(_image(caption="hello", width=800, height=600))
    plan = wa_media.plan_save(media, tmp_path, chat="Varshith", sender="919999")
    payload = b"\xff\xd8\xff\xe0 pretend jpeg"

    meta = wa_media.write_media(plan, payload)

    assert plan.path.read_bytes() == payload            # the content itself
    written = json.loads(plan.metadata_path.read_text(encoding="utf-8"))
    assert written["kind"] == "image"
    assert written["caption"] == "hello"
    assert written["chat"] == "Varshith"
    assert written["actual_bytes"] == len(payload)
    assert written["saved_as"] == plan.path.name
    assert "size_mismatch" not in written
    assert meta == written


def test_a_truncated_download_is_flagged_not_silently_accepted(tmp_path):
    """WhatsApp tells us how big the file should be; a short read means a
    corrupt file, and saying nothing would leave a broken file on disk."""
    media = wa_media.classify(_image(fileLength=999999))
    plan = wa_media.plan_save(media, tmp_path, chat="X")

    meta = wa_media.write_media(plan, b"only a few bytes")

    assert meta["size_mismatch"] is True
    assert meta["declared_bytes"] == 999999
    assert meta["actual_bytes"] == 16


def test_empty_metadata_fields_are_left_out(tmp_path):
    """A photo has no page count and a document has no duration — writing
    zeroes for them makes the sidecar noise."""
    media = wa_media.classify(_image())
    plan = wa_media.plan_save(media, tmp_path, chat="X")
    meta = wa_media.write_media(plan, b"x")

    assert "page_count" not in meta
    assert "seconds" not in meta
    assert "caption" not in meta


# --- end to end (only the network call stubbed) ------------------------------

def test_save_from_event_downloads_and_writes(tmp_path):
    client = _StubClient(b"REAL BYTES HERE")
    msg = _image(caption="from the beach", width=100, height=50)

    meta = wa_media.save_from_event(client, msg, tmp_path, chat="Nagen US",
                                    sender="919999", message_id="MSG123")

    assert client.calls == 1
    saved = tmp_path / "image" / meta["saved_as"]
    assert saved.read_bytes() == b"REAL BYTES HERE"
    assert meta["caption"] == "from the beach"


def test_save_from_event_ignores_a_text_message(tmp_path):
    """A text message must not cost a download call or create an empty file."""
    client = _StubClient()
    msg = Message()
    msg.conversation = "hello there"

    assert wa_media.save_from_event(client, msg, tmp_path) is None
    assert client.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_each_kind_lands_in_its_own_folder(tmp_path):
    client = _StubClient()
    photo = _image()
    voice = Message()
    voice.audioMessage.mimetype = "audio/ogg"
    voice.audioMessage.PTT = True
    doc = Message()
    doc.documentMessage.mimetype = "application/pdf"
    doc.documentMessage.fileName = "x.pdf"

    for msg in (photo, voice, doc):
        wa_media.save_from_event(client, msg, tmp_path, chat="X")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["document", "image", "voice"]
