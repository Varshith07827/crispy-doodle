"""Comparing what we asked the compose box to hold against what it reads back.

Exact equality was the bug: WhatsApp's contenteditable does not necessarily
read a typed newline back as "\\n", so a multi-line AI answer never verified.
The send was reported as "could not type into the compose box", the text was
left sitting there, and every retry typed the whole thing again — the
"it keeps rewriting the message and never sends it" failure.
"""

import pytest

from winspark.connectors.whatsapp_group_sender import (
    _PASTE_THRESHOLD_CHARS,
    _normalize_compose_text,
)


def _matches(actual: str, expected: str) -> bool:
    return _normalize_compose_text(actual) == _normalize_compose_text(expected)


def test_newlines_read_back_as_spaces_still_count_as_typed():
    typed = "On August 3, 2026, in Goa:\nlight rain, 24-29C.\nYellow watch until 1am."
    read_back = "On August 3, 2026, in Goa: light rain, 24-29C. Yellow watch until 1am."
    assert _matches(read_back, typed)
    assert read_back.strip() != typed.strip()   # the old exact compare said "missing"


def test_line_ending_and_trailing_space_differences_are_tolerated():
    assert _matches("a\r\nb", "a\nb")
    assert _matches("hello   world", "hello world")
    assert _matches("  padded  ", "padded")
    assert _matches("a\n\n\nb", "a b")


def test_genuinely_different_text_still_fails():
    """Tolerance must not become "anything passes" — a truncated or empty box
    has to be caught, or a half-typed message gets sent."""
    assert not _matches("", "hello there")
    assert not _matches("hello", "hello there")
    assert not _matches("hello there", "hello")
    assert not _matches("hello world", "hello wolrd")


def test_emoji_are_ignored_on_both_sides():
    """WhatsApp's compose box reads an emoji back as U+FFFC (object
    replacement) or drops it, so comparing them is comparing noise. Since the
    AI style prompt allows one emoji, a `!name` reply routinely contains one —
    and the failed verification put the send into a paste/retype/clear loop.
    Ignoring emoji means the worst case is a message whose emoji alone went
    missing, which beats never sending it."""
    assert _matches("hey 💖 there", "hey 💖 there")
    assert _matches("hey 💖\nthere", "hey 💖 there")
    assert _matches("hey ￼ there", "hey 💖 there")   # read back as U+FFFC
    assert _matches("hey there", "hey 💖 there")          # dropped entirely
    # Everything that isn't emoji is still compared strictly.
    assert not _matches("hey 💖 you", "hey 💖 there")


# --- "is the box empty?" is proof-of-send, so it has its own rules -----------

class _Box:
    def __init__(self, text):
        self.text = text


def _blank(text: str) -> bool:
    from winspark.connectors import whatsapp_group_sender as gs

    original = gs._read_compose_text
    gs._read_compose_text = lambda c: c.text
    try:
        return gs._compose_is_blank(_Box(text))
    finally:
        gs._read_compose_text = original


@pytest.mark.parametrize("text", [
    "", " ", "\n", "\n ",
    "￼",              # U+FFFC left where an emoji was
    "￼\n", " ￼ ",
    "️", "︎",          # stray variation selectors
])
def test_leftovers_from_a_cleared_box_count_as_empty(text):
    """An emptied box can read back as the object-replacement placeholder, which
    .strip() does not remove — so the box never looked empty, proof-of-send
    never arrived, and a reply carrying an emoji looped paste/clear."""
    assert _blank(text) is True


@pytest.mark.parametrize("text", [
    "hello",
    "👍",                    # an emoji-ONLY message is still a message
    "💖 hi",
    "a",
])
def test_a_box_that_still_holds_a_message_is_not_empty(text):
    """Real emoji are not ignored here, unlike in the comparison helper:
    calling an emoji-only reply "sent" would silently drop it."""
    assert _blank(text) is False


def test_everything_is_pasted_there_is_no_typing_threshold():
    """Pasting once applied only above 200 characters. An AI reply is one or
    two sentences, so replies never reached it and always took the typing
    route — the one that drops keystrokes, mangles emoji into surrogate halves
    and rewrites the box on every retry. Typing is now only the fallback."""
    assert _PASTE_THRESHOLD_CHARS == 0
