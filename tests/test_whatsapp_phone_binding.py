"""Binding a chat by phone number or contact name (not just the exact chat
name). Pure logic — the UIA reads are stubbed, so this runs anywhere."""

import asyncio

import pytest

from winspark.connectors import whatsapp_group_sender as gs
from winspark.connectors.whatsapp import WhatsAppChatRow
from winspark.connectors.whatsapp_group_sender import WhatsAppGroupSender


def _row(name, raw=None):
    return WhatsAppChatRow(chat_name=name, timestamp_text="", last_message="",
                           unread_count=0, raw_text=raw if raw is not None else name)


# --- phone key normalization -------------------------------------------------

def test_phone_key_normalizes_country_code_and_punctuation():
    assert gs._phone_key("+91 79811 49423") == "7981149423"
    assert gs._phone_key("07981149423") == "7981149423"
    assert gs._phone_key("(798) 114-9423") == "7981149423"
    # A name is not a phone number.
    assert gs._phone_key("Vishnu Cr Gvp") == ""
    assert gs._phone_key("Karthik") == ""
    # Too few digits -> not a phone number.
    assert gs._phone_key("Room 402") == ""


def test_looks_like_phone_number():
    assert gs.looks_like_phone_number("+91 79811 49423") is True
    assert gs.looks_like_phone_number("Karthik") is False


# --- matching ----------------------------------------------------------------

def test_match_by_number_finds_a_number_named_chat():
    rows = [_row("Karthik"), _row("+91 79811 49423 (You)", raw="+91 79811 49423 (You) hiii")]
    assert gs._match_chat_row(rows, "7981149423").chat_name == "+91 79811 49423 (You)"
    assert gs._match_chat_row(rows, "+91 79811 49423").chat_name == "+91 79811 49423 (You)"


def test_match_by_name_still_works():
    rows = [_row("Karthik"), _row("Vishnu Cr Gvp")]
    assert gs._match_chat_row(rows, "Karthik").chat_name == "Karthik"
    assert gs._match_chat_row(rows, "Vishnu Cr Gv").chat_name == "Vishnu Cr Gvp"  # fuzzy


def test_first_real_result_skips_section_headers():
    rows = [_row("Chats"), _row(""), _row("Papa")]
    assert gs._first_real_result(rows).chat_name == "Papa"
    assert gs._first_real_result([_row("Contacts")]) is None


# --- resolve: a saved contact searched by number -----------------------------

class _Sta:
    def __init__(self):
        self.action_lock = asyncio.Lock()

    async def invoke_async(self, fn):
        return fn()


class _Connector:
    def __init__(self, recents):
        self._recents = recents

    async def find_window_async(self):
        return 4242

    async def read_chat_rows_async(self, handle):
        return list(self._recents)


@pytest.mark.asyncio
async def test_number_resolves_a_saved_contact_via_top_search_result(monkeypatch):
    # Recents has no match for the number; search surfaces the saved contact
    # "Papa" (whose row shows the NAME, not the number) — the number search must
    # still resolve to it via the top-result fallback.
    sender = WhatsAppGroupSender(_Connector([_row("Karthik")]), _Sta())
    monkeypatch.setattr(gs, "_search_and_read_rows_sync", lambda h, q: [_row("Chats"), _row("Papa")])
    monkeypatch.setattr(gs, "_clear_search_sync", lambda h: None)

    handle, row = await sender.resolve_chat_row_async("+91 79811 49423")
    assert handle == 4242
    assert row is not None and row.chat_name == "Papa"


@pytest.mark.asyncio
async def test_unknown_name_does_not_grab_a_random_top_result(monkeypatch):
    # A non-phone name that isn't found must return None, NOT the first result —
    # the top-result shortcut is only for unambiguous number searches.
    sender = WhatsAppGroupSender(_Connector([_row("Karthik")]), _Sta())
    monkeypatch.setattr(gs, "_search_and_read_rows_sync", lambda h, q: [_row("Someone Else")])
    monkeypatch.setattr(gs, "_clear_search_sync", lambda h: None)

    handle, row = await sender.resolve_chat_row_async("Nonexistent Person")
    assert row is None


# --- emoji-tolerant search + open-chat short-circuit -------------------------

def test_search_query_strips_emoji_but_keeps_names_and_numbers():
    assert gs._search_query("Papa \U0001F49C") == "Papa"
    assert gs._search_query("\U0001F389 Party \U0001F389") == "Party"
    assert gs._search_query("Vishnu Cr Gvp") == "Vishnu Cr Gvp"
    assert gs._search_query("+91 79811 49423") == "+91 79811 49423"
    assert gs._search_query("A&B Corp") == "A&B Corp"          # non-emoji symbols kept
    assert gs._search_query("\U0001F49C") == "\U0001F49C"      # emoji-only -> fall back


@pytest.mark.asyncio
async def test_open_chat_short_circuits_when_already_open(monkeypatch):
    # The chat is already the open conversation -> success WITHOUT resolving or
    # foregrounding (the fix for "Couldn't open this chat" on an open chat).
    resolved = []
    sender = WhatsAppGroupSender(_Connector([]), _Sta())

    async def resolve(name):
        resolved.append(name)
        return 4242, None  # resolve would fail — must not be reached

    sender.resolve_chat_row_async = resolve
    monkeypatch.setattr(gs, "_chat_already_open", lambda h, t: True)

    assert await sender.open_chat_async("Papa \U0001F49C") is True
    assert resolved == []  # short-circuited before any resolve


@pytest.mark.asyncio
async def test_open_chat_resolves_when_not_already_open(monkeypatch):
    monkeypatch.setattr(gs, "_chat_already_open", lambda h, t: False)
    monkeypatch.setattr(gs, "_open_chat_sync", lambda h, raw, name: True)
    sender = WhatsAppGroupSender(_Connector([]), _Sta())

    async def resolve(name):
        return 4242, _row("Papa")

    sender.resolve_chat_row_async = resolve
    assert await sender.open_chat_async("Papa") is True
