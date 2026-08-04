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


# --- a received photo names nothing at all -----------------------------------
#
# Found live against a real photo: the bubble's ImageControl has Name "" and
# there is no Play/Download/"Photo" control anywhere in it, so matching on
# control names classified it as a plain message. The photo was silently
# dropped from chat memory and never captured. A sent DOCUMENT worked only
# because it carries a "Download" button. Real rects/areas below.

def _photo_bubble(caption="", image_area_rect=(699, 749, 1145, 1000)):
    """A received photo bubble as WhatsApp Desktop really exposes it: an unnamed
    ImageControl, a timestamp, the read ticks, and nothing else nameable."""
    children = [
        _Node("ImageControl", "", _Rect(*image_area_rect)),        # the photo — no name
        _Node("ImageControl", "wds-ic-read", _Rect(1785, 643, 1807, 666)),  # ticks, 506px²
        _Node("TextControl", "1:54 pm"),
    ]
    if caption:
        children.insert(1, _Node("TextControl", caption))
    return _Node("DataItemControl", children=children)


def test_an_unnamed_photo_is_still_recognised_as_a_photo():
    display, kind, note, time_text, rect = whatsapp._enrich_media(_photo_bubble(), "")

    assert kind == MEDIA_PHOTO
    assert display == "[Photo]"
    assert rect == (699, 749, 1145, 1000)      # the capture region
    assert time_text == "1:54 pm"
    assert note == ""


def test_a_caption_rides_along_with_an_unnamed_photo():
    display, kind, _note, _t, _r = whatsapp._enrich_media(_photo_bubble(), "look at this")
    assert (kind, display) == (MEDIA_PHOTO, "[Photo] look at this")


def test_the_word_photo_as_body_text_is_not_repeated_in_the_placeholder():
    display, _k, note, _t, _r = whatsapp._enrich_media(_photo_bubble(), "Photo")
    assert display == "[Photo]"      # not "[Photo] Photo"
    assert note == ""


def test_read_ticks_and_emoji_do_not_turn_a_text_message_into_a_photo():
    """The fallback keys off image SIZE, so the threshold has to sit above the
    decorations every bubble carries. Measured: ticks 506px², emoji 1,122px²."""
    row = _Node("DataItemControl", children=[
        _Node("TextControl", "just talking"),
        _Node("ImageControl", "wds-ic-read", _Rect(1785, 643, 1807, 666)),   # 506
        _Node("ImageControl", "", _Rect(38, 463, 72, 496)),                  # 1,122
        _Node("TextControl", "1:54 pm"),
    ])
    display, kind, _note, _t, rect = whatsapp._enrich_media(row, "just talking")

    assert kind == ""                    # still a plain message
    assert display == "just talking"
    assert rect is None


def test_a_named_attachment_still_wins_over_the_size_fallback():
    """A document bubble also contains images (the file-type icon, the ticks).
    Its "Download" button must still classify it as a document, not a photo."""
    row = _Node("DataItemControl", children=[
        _Node("ButtonControl", "Download"),
        _Node("TextControl", "winSpark.exe"),
        _Node("ImageControl", "", _Rect(0, 0, 400, 400)),   # big, but not the point
        _Node("TextControl", "6:58 am"),
    ])
    # The filename rides in as body text, which is where the live bubble puts it
    # (and why the note keeps its capitals rather than the lowercased signal name).
    display, kind, note, _t, rect = whatsapp._enrich_media(row, "winSpark.exe")

    assert kind == MEDIA_DOCUMENT
    assert (display, note) == ("[Document: winSpark.exe]", "winSpark.exe")
    assert rect is None                  # a document has no pixels to capture


# --- a voice note's duration lives on its slider, not in any text ------------
#
# Found live: a real voice-note bubble contains only a "Play voice message"
# button and a "Voice note progress slider". No duration text exists to scan
# for, so the placeholder read a bare "[Voice note]". The slider carries it as
# ValuePattern "0:00/0:04" and RangeValuePattern Maximum 4.0.

class _Pattern:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _SliderNode(_Node):
    """A slider exposing the two patterns WhatsApp's voice note really has."""

    def __init__(self, name="Voice note progress slider", value=None, maximum=None):
        super().__init__("SliderControl", name)
        self._value, self._maximum = value, maximum

    def GetPattern(self, pattern_id):  # noqa: N802 - uiautomation API
        import uiautomation as auto

        if pattern_id == auto.PatternId.ValuePattern:
            if self._value is None:
                raise RuntimeError("no ValuePattern")
            return _Pattern(Value=self._value)
        if pattern_id == auto.PatternId.RangeValuePattern:
            if self._maximum is None:
                raise RuntimeError("no RangeValuePattern")
            return _Pattern(Maximum=self._maximum)
        raise RuntimeError("unsupported pattern")


def _voice_bubble(slider):
    return _Node("DataItemControl", children=[
        _Node("ButtonControl", "Play voice message"),
        slider,
        _Node("TextControl", "6:59 pm"),
    ])


def test_voice_duration_is_read_from_the_slider_value():
    row = _voice_bubble(_SliderNode(value="0:00/0:04", maximum=4.0))
    display, kind, note, _t, _r = whatsapp._enrich_media(row, "")

    assert kind == MEDIA_VOICE
    assert note == "0:04"
    assert display == "[Voice note · 0:04]"


def test_voice_duration_falls_back_to_the_slider_range():
    """If the formatted string is unavailable, the range maximum is seconds."""
    row = _voice_bubble(_SliderNode(value=None, maximum=75.0))
    _display, _kind, note, _t, _r = whatsapp._enrich_media(row, "")
    assert note == "1:15"


def test_a_voice_note_with_no_readable_duration_still_reads_as_a_voice_note():
    row = _voice_bubble(_SliderNode(value=None, maximum=None))
    display, kind, note, _t, _r = whatsapp._enrich_media(row, "")
    assert (kind, note, display) == (MEDIA_VOICE, "", "[Voice note]")


def test_a_duration_in_the_bubble_text_still_wins_over_the_slider():
    """Where WhatsApp does render a duration, keep using it — the slider is a
    fallback, not a replacement."""
    row = _Node("DataItemControl", children=[
        _Node("ButtonControl", "Play voice message"),
        _Node("TextControl", "0:12"),
        _SliderNode(value="0:00/0:99", maximum=99.0),
    ])
    _display, _kind, note, _t, _r = whatsapp._enrich_media(row, "0:12")
    assert note == "0:12"


def test_an_unrelated_slider_is_not_mistaken_for_a_voice_note():
    """The window also has a 'Side Panel Resize Handle (draggable)' slider."""
    row = _voice_bubble(_SliderNode(name="Side Panel Resize Handle (draggable)",
                                    value="", maximum=0.0))
    _display, kind, note, _t, _r = whatsapp._enrich_media(row, "")
    assert (kind, note) == (MEDIA_VOICE, "")     # still a voice note, just no duration


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
