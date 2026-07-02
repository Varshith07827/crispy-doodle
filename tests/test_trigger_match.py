"""Tests for the literal trigger matcher (the fallback used when OpenAI isn't
configured)."""

from winspark.connectors.trigger_match import literal_match


def test_substring_matches():
    assert literal_match("invoice", "Here is the invoice for March") is True


def test_all_significant_words_present_matches():
    assert literal_match("coming party", "hey are you coming to the party?") is True


def test_missing_a_significant_word_does_not_match():
    assert literal_match("coming to the party", "what's for dinner?") is False


def test_case_insensitive():
    assert literal_match("Invoice", "the INVOICE is attached") is True


def test_empty_trigger_or_message_never_matches():
    assert literal_match("", "anything") is False
    assert literal_match("something", "") is False


def test_short_words_only_falls_back_to_any_overlap():
    # "ok" is below the significant-word length; matches on any-word overlap.
    assert literal_match("ok", "ok sounds good") is True
    assert literal_match("ok", "no thanks") is False
