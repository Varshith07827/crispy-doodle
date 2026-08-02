"""Addressing the bot by name: "!winspark what's the weather".

The whole point of this matcher is that it is STRICT where `literal_match` is
lenient. `literal_match` exists to answer "is this message roughly about the
trigger phrase?"; this one answers "did someone actually call the bot?" — and
getting that wrong means barging into a conversation nobody invited it to.
"""

import pytest

from winspark.constants import AI_COMMAND_PREFIX
from winspark.connectors.trigger_match import command_match


@pytest.mark.parametrize("message, query", [
    ("!winspark what's the weather", "what's the weather"),
    ("!winspark", ""),                                   # bare call -> greet
    ("hey !winspark summarise that", "summarise that"),
    ("!WinSpark how are you", "how are you"),            # case-insensitive
    ("!winspark, what's up", "what's up"),               # punctuation after the name
    ("!winspark: explain this", "explain this"),
    ("  !winspark   spaced   out  ", "spaced   out"),
    ("what's the weather !winspark", "what's the weather"),   # command written last
    ("hey !winspark", ""),                               # filler only -> nothing asked
    ("!winspark what about docs/winspark/readme.md?", "what about docs/winspark/readme.md?"),
])
def test_addressed_messages_match_and_yield_the_question(message, query):
    matched, asked = command_match("winspark", message)
    assert matched is True
    assert asked == query


@pytest.mark.parametrize("message", [
    "I was reading about winspark yesterday",   # the case that makes a bare-word match unusable
    "winspark is great",                        # no prefix at all
    "!winsparkling water please",               # word boundary: not our name
    "!winspar",                                 # incomplete
    "docs/winspark/readme.md",                  # path — the reason "/" was rejected as a prefix
    "http://example.com/winspark",              # URL — same
    "that's amazing!winspark rocks",            # prefix glued to the previous word
    "!otherbot do something",                   # a different bot's name
    "",
    "   ",
])
def test_unaddressed_messages_never_match(message):
    matched, asked = command_match("winspark", message)
    assert matched is False
    assert asked == ""


def test_blank_command_word_never_matches():
    """An unconfigured binding must stay silent rather than answer everything."""
    assert command_match("", "!winspark hello") == (False, "")
    assert command_match("   ", "anything at all") == (False, "")


def test_prefix_typed_into_the_command_word_is_tolerated():
    """People will naturally type "!winspark" into the settings field. Storing
    it raw would make the matcher hunt for "!!winspark"."""
    for configured in ("!winspark", "@winspark", "/winspark", "winspark"):
        assert command_match(configured, "!winspark hi there") == (True, "hi there")


def test_prefix_is_the_configured_one():
    assert AI_COMMAND_PREFIX == "!"
    # The prefix is a parameter, so switching it is a one-line change and the
    # old one stops working — no accidental dual-triggering.
    assert command_match("winspark", "#winspark hi", prefix="#") == (True, "hi")
    assert command_match("winspark", "!winspark hi", prefix="#") == (False, "")


def test_name_with_regex_characters_is_matched_literally():
    """The name goes into a regex, so it must be escaped — otherwise a name like
    "c++" is a pattern-compile error rather than a name."""
    assert command_match("c++", "!c++ explain pointers") == (True, "explain pointers")


def test_only_the_first_call_in_a_message_is_used():
    matched, asked = command_match("winspark", "!winspark one !winspark two")
    assert matched is True
    assert asked == "one !winspark two"
