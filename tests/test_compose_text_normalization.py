"""Comparing what we asked the compose box to hold against what it reads back.

Exact equality was the bug: WhatsApp's contenteditable does not necessarily
read a typed newline back as "\\n", so a multi-line AI answer never verified.
The send was reported as "could not type into the compose box", the text was
left sitting there, and every retry typed the whole thing again — the
"it keeps rewriting the message and never sends it" failure.
"""

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


def test_emoji_and_unicode_survive_normalization():
    assert _matches("hey 💖 there", "hey 💖 there")
    assert _matches("hey 💖\nthere", "hey 💖 there")
    assert not _matches("hey there", "hey 💖 there")


def test_paste_threshold_is_where_typing_gets_painful():
    """At the measured-safe 30ms/char, the threshold is the point past which
    typing costs seconds — and gets paid again on every send retry."""
    assert _PASTE_THRESHOLD_CHARS >= 100
    seconds_at_threshold = _PASTE_THRESHOLD_CHARS * 0.03
    assert seconds_at_threshold <= 10
