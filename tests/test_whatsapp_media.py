"""Tests for reading attachments (photos, voice notes, documents) out of the
WhatsApp accessibility tree — the metadata winSpark can name even though the
tree never hands over the bytes — and for the opt-in scroll-deep chat-list read.

The UIA tree is stubbed with plain fake nodes, so these run anywhere. They test
the classification/placeholder/enrichment logic and the deep-read accumulate/
dedupe/stop loop — not the live COM calls, which need real WhatsApp."""

import pytest

from winspark.connectors import whatsapp
from winspark.connectors.whatsapp import (
    MEDIA_DOCUMENT,
    MEDIA_PHOTO,
    MEDIA_VOICE,
    media_placeholder,
)


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Node:
    """A minimal stand-in for a uiautomation control."""

    def __init__(self, control_type, name="", rect=None, children=None):
        self.ControlTypeName = control_type
        self.Name = name
        self._rect = rect
        self._children = children or []

    @property
    def BoundingRectangle(self):
        if self._rect is None:
            raise RuntimeError("no rect")
        return self._rect

    def GetChildren(self):
        return self._children


# --- placeholder formatting --------------------------------------------------

def test_media_placeholder_shapes():
    assert media_placeholder(MEDIA_VOICE, "0:12") == "[Voice note · 0:12]"
    assert media_placeholder(MEDIA_VOICE, "") == "[Voice note]"
    assert media_placeholder(MEDIA_DOCUMENT, "report.pdf") == "[Document: report.pdf]"
    assert media_placeholder(MEDIA_PHOTO, "look at this") == "[Photo] look at this"
    assert media_placeholder(MEDIA_PHOTO, "") == "[Photo]"
    assert media_placeholder("", "x") == ""  # not a media kind


# --- classification + enrichment ---------------------------------------------

def test_voice_note_is_classified_with_duration_and_time():
    row = _Node("DataItemControl", children=[
        _Node("ButtonControl", "Play voice message"),
        _Node("TextControl", "0:12"),
        _Node("TextControl", "9:21 pm"),
    ])
    display, kind, note, time_text, rect = whatsapp._enrich_media(row, "0:12")
    assert kind == MEDIA_VOICE
    assert note == "0:12"
    assert display == "[Voice note · 0:12]"
    assert time_text == "9:21 pm"
    assert rect is None  # voice has no picture to capture


def test_photo_with_caption_keeps_caption_and_exposes_rect():
    row = _Node("DataItemControl", children=[
        _Node("ImageControl", "Photo", rect=_Rect(500, 100, 800, 400)),
        _Node("TextControl", "look at this"),
        _Node("TextControl", "8:00 am"),
    ])
    display, kind, note, time_text, rect = whatsapp._enrich_media(row, "look at this")
    assert kind == MEDIA_PHOTO
    assert note == "look at this"
    assert display == "[Photo] look at this"
    assert time_text == "8:00 am"
    assert rect == (500, 100, 800, 400)  # the picture region, for thumbnailing


def test_bare_photo_survives_with_no_caption():
    # A photo with no caption produced NO text before this change, so the whole
    # bubble was dropped from memory. It must now survive as "[Photo]".
    row = _Node("DataItemControl", children=[
        _Node("ImageControl", "Photo", rect=_Rect(500, 100, 800, 400)),
    ])
    display, kind, note, _time, rect = whatsapp._enrich_media(row, "Photo")
    assert kind == MEDIA_PHOTO
    assert display == "[Photo]"
    assert note == ""
    assert rect == (500, 100, 800, 400)


def test_document_pulls_out_the_filename():
    row = _Node("DataItemControl", children=[
        _Node("ButtonControl", "Download"),
        _Node("TextControl", "report.pdf"),
        _Node("TextControl", "2 MB"),
    ])
    display, kind, note, _time, rect = whatsapp._enrich_media(row, "report.pdf 2 MB")
    assert kind == MEDIA_DOCUMENT
    assert note == "report.pdf"
    assert display == "[Document: report.pdf]"
    assert rect is None  # a document isn't a capturable picture


def test_plain_text_is_not_treated_as_media():
    row = _Node("DataItemControl", children=[
        _Node("TextControl", "hello there"),
        _Node("TextControl", "7:15 pm"),
    ])
    display, kind, note, time_text, rect = whatsapp._enrich_media(row, "hello there")
    assert kind == "" and note == "" and rect is None
    assert display == "hello there"
    assert time_text == "7:15 pm"


def test_tiny_glyphs_do_not_count_as_a_capturable_picture():
    # A 12x12 emoji/icon image must not be mistaken for a photo region.
    row = _Node("DataItemControl", children=[
        _Node("ImageControl", "🚀", rect=_Rect(10, 10, 22, 22)),
        _Node("TextControl", "nice"),
    ])
    assert whatsapp._largest_image_rect(row) is None


# --- scroll-deep chat-list read ----------------------------------------------

@pytest.fixture
def staged_grid(monkeypatch):
    """Drive _read_chat_rows_deep_sync against staged row batches, one per scroll
    step, so the accumulate/dedupe/stop logic can be exercised without COM."""
    monkeypatch.setattr(whatsapp, "_require_win32", lambda: None)
    monkeypatch.setattr(whatsapp, "_find_chat_grid", lambda *a, **k: object())
    monkeypatch.setattr(whatsapp, "parse_chat_row", lambda name: {
        "chat_name": name, "timestamp_text": "", "last_message": "", "unread_count": 0, "raw_text": name,
    })

    def configure(batches):
        state = {"i": 0}
        monkeypatch.setattr(whatsapp, "_iter_grid_row_controls",
                            lambda grid: [_Node("DataItemControl", n) for n in batches[state["i"]]])

        def scroll(grid, down=True):
            if state["i"] + 1 < len(batches):
                state["i"] += 1
                return True
            return False  # nothing more to reveal

        monkeypatch.setattr(whatsapp, "_scroll_chat_list_sync", scroll)
        monkeypatch.setattr(whatsapp, "_scroll_chat_list_to_top_sync", lambda grid: None)
        monkeypatch.setattr(whatsapp.time, "sleep", lambda *_a: None)

    return configure


def test_deep_read_accumulates_across_scrolls_and_dedupes(staged_grid):
    # Overlapping batches (Bob appears twice) — the deep read must union them,
    # keep first-seen order, and not duplicate.
    staged_grid([
        ["Alice", "Bob"],
        ["Bob", "Carol"],
        ["Dave"],
    ])
    rows = whatsapp._read_chat_rows_deep_sync(1, max_scrolls=6)
    assert [r.chat_name for r in rows] == ["Alice", "Bob", "Carol", "Dave"]


def test_deep_read_stops_when_a_scroll_reveals_nothing_new(staged_grid):
    # If a scroll surfaces only already-seen chats, the read stops there.
    staged_grid([
        ["Alice", "Bob"],
        ["Alice", "Bob"],   # no new rows -> stop, don't spin to max_scrolls
        ["Should", "Not", "Reach"],
    ])
    rows = whatsapp._read_chat_rows_deep_sync(1, max_scrolls=6)
    assert [r.chat_name for r in rows] == ["Alice", "Bob"]
