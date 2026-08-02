"""Tests parse_chat_row against real WhatsApp Desktop chat-list rows,
captured live from a running instance while building this connector (not
invented examples) — see whatsapp.py's module docstring for how they were
captured (GridPattern.GetItem() on the "Chat list" DataGrid).

Runs on any platform — pure string parsing, no UI Automation involved.
"""

from winspark.connectors.whatsapp_row_parser import parse_chat_row


def test_unread_group_chat_with_count_prefix():
    raw = "4 unread messages Vishnu Cr Gvp Yesterday ekada grp names navi unaye just for example ki steps lo include chesanu"
    result = parse_chat_row(raw)

    assert result["unread_count"] == 4
    assert result["chat_name"] == "Vishnu Cr Gvp"
    assert result["timestamp_text"] == "Yesterday"
    assert result["last_message"] == "ekada grp names navi unaye just for example ki steps lo include chesanu"
    assert not result["is_pinned"]


def test_simple_chat_with_no_flags():
    raw = "Kushal Pavan Asuri Yesterday Ok"
    result = parse_chat_row(raw)

    assert result["unread_count"] == 0
    assert result["chat_name"] == "Kushal Pavan Asuri"
    assert result["timestamp_text"] == "Yesterday"
    assert result["last_message"] == "Ok"


def test_another_simple_chat():
    raw = "Dhishitha Gvp Yesterday Okkk"
    result = parse_chat_row(raw)

    assert result["chat_name"] == "Dhishitha Gvp"
    assert result["last_message"] == "Okkk"


def test_pinned_chat_with_url_message():
    raw = "CSE - C Yesterday Chaitu: https://chat.whatsapp.com/JjceXdjWvbB3gNwCmzRVI2?s=cl&p=a&ilr=4 Pinned chat"
    result = parse_chat_row(raw)

    assert result["chat_name"] == "CSE - C"
    assert result["timestamp_text"] == "Yesterday"
    assert result["last_message"] == "Chaitu: https://chat.whatsapp.com/JjceXdjWvbB3gNwCmzRVI2?s=cl&p=a&ilr=4"
    assert result["is_pinned"] is True
    assert result["is_starred"] is False


def test_pinned_and_starred_chat_strips_both_trailing_flags():
    raw = "+91 79811 49423 (You) Yesterday Winspark.zip Pinned chat Starred chat"
    result = parse_chat_row(raw)

    assert result["last_message"] == "Winspark.zip"
    assert result["timestamp_text"] == "Yesterday"
    assert result["is_pinned"] is True
    assert result["is_starred"] is True


def test_no_anchor_falls_back_to_whole_text_as_chat_name():
    result = parse_chat_row("Just some text with no day or time in it")

    assert result["chat_name"] == "Just some text with no day or time in it"
    assert result["timestamp_text"] == ""
    assert result["last_message"] == ""


def test_raw_text_is_preserved_verbatim():
    raw = "  Kushal Pavan Asuri Yesterday Ok  "
    result = parse_chat_row(raw)

    assert result["raw_text"] == raw


def test_view_status_prefix_is_stripped_with_unread_count():
    # A contact with a posted status: WhatsApp prepends "View status" (the avatar
    # button) ahead of the unread count and name. Both must be stripped so the
    # name is just "Hasini", not "View status 2 unread messages Hasini".
    result = parse_chat_row(
        "View status 2 unread messages Hasini 10:12 pm Anaya amma ki maggi tinna ani telu..."
    )
    assert result["chat_name"] == "Hasini"
    assert result["unread_count"] == 2
    assert result["timestamp_text"] == "10:12 pm"


def test_view_status_prefix_without_unread():
    result = parse_chat_row("View status Karthik 10:05 pm ABHI INTRO final.pdf")
    assert result["chat_name"] == "Karthik"
    assert result["unread_count"] == 0


def test_chat_named_normally_is_unaffected_by_status_stripping():
    # No "View status" prefix — behaviour unchanged.
    result = parse_chat_row("4 unread messages Vishnu Cr Gvp Yesterday ekada grp names")
    assert result["chat_name"] == "Vishnu Cr Gvp"
    assert result["unread_count"] == 4
