"""Tests the WhatsAppChatNameRules port — the fuzzy chat-name matching used to
resolve a bound group name against a possibly-truncated sidebar row. Cases
ported from the upstream WhatsAppChatNameRulesTests.cs. Pure logic, any platform.
"""

import pytest

from winspark.connectors.whatsapp_chat_name_rules import chat_names_match, is_system_or_list_view_title


@pytest.mark.parametrize(
    "requested,candidate",
    [
        ("winspark", "winspark"),
        ("Infosys", "infosys"),          # case-insensitive
        ("Novus Tech Group", "Novus Tech Gr"),  # truncated sidebar label
    ],
)
def test_matches_exact_or_truncated(requested, candidate):
    assert chat_names_match(requested, candidate) is True


@pytest.mark.parametrize(
    "requested,candidate",
    [
        ("Novus Tech Group", "NOVUS"),          # too short / low coverage
        ("Novus Tech Group", "NOVUS CLUB -2.0"),  # shared first word only
        ("Novus Tech Group", "Novus Club"),       # only first word matches
        ("heloo", "Infosys"),                     # unrelated
        ("winspark", "Infosys"),                  # unrelated
    ],
)
def test_rejects_different_chats(requested, candidate):
    assert chat_names_match(requested, candidate) is False


def test_empty_or_none_never_matches():
    assert chat_names_match("", "Infosys") is False
    assert chat_names_match("Infosys", "") is False
    assert chat_names_match(None, "Infosys") is False
    assert chat_names_match("Infosys", None) is False


def test_trailing_ellipsis_is_normalized_away():
    assert chat_names_match("Vishnu Cr Gvp", "Vishnu Cr Gvp…") is True


def test_is_system_or_list_view_title():
    assert is_system_or_list_view_title("Chats") is True
    assert is_system_or_list_view_title("Archived") is True
    assert is_system_or_list_view_title("Settings") is True
    assert is_system_or_list_view_title("") is True
    assert is_system_or_list_view_title(None) is True
    assert is_system_or_list_view_title("Vishnu Cr Gvp") is False
