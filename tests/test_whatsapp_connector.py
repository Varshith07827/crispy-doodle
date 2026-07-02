"""Windows-only, read-only smoke test for WhatsAppConnector against a real,
already-running WhatsApp Desktop instance. Skipped entirely if WhatsApp isn't
running — this deliberately never launches or closes WhatsApp itself (unlike
the synthetic-window tests elsewhere in this suite), since driving someone's
real messaging app in an automated test is a different risk profile than a
disposable EDIT control. Never sends a message or clicks anything; only
reads via UI Automation.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires pywin32 + uiautomation on a real Windows session")


@pytest.fixture(scope="module")
def connector(manager):
    from winspark.connectors.whatsapp import WhatsAppConnector

    return WhatsAppConnector(manager)


@pytest.fixture(scope="module")
async def window_handle(connector):
    hwnd = await connector.find_window_async()
    if hwnd is None:
        pytest.skip("WhatsApp Desktop is not running on this machine")
    return hwnd


@pytest.mark.asyncio
async def test_find_window_returns_a_real_hwnd(window_handle):
    import win32gui

    assert window_handle > 0
    assert win32gui.IsWindow(window_handle)


@pytest.mark.asyncio
async def test_unread_badge_count_is_non_negative_int(connector, window_handle):
    count = await connector.get_unread_badge_count_async(window_handle)
    assert isinstance(count, int)
    assert count >= 0


@pytest.mark.asyncio
async def test_read_chat_rows_returns_parsed_rows(connector, window_handle):
    rows = await connector.read_chat_rows_async(window_handle)

    assert len(rows) > 0, "expected at least one chat in the list"
    for row in rows[:20]:
        assert row.chat_name
        assert row.raw_text
        assert row.unread_count >= 0


@pytest.mark.asyncio
async def test_unread_chats_are_a_subset_of_all_chats_with_positive_count(connector, window_handle):
    all_rows = await connector.read_chat_rows_async(window_handle)
    unread_rows = await connector.get_unread_chats_async(window_handle)

    assert all(r.unread_count > 0 for r in unread_rows)
    assert len(unread_rows) <= len(all_rows)


@pytest.mark.asyncio
async def test_unread_badge_count_is_at_least_the_unread_chats_found_in_the_realized_range(connector, window_handle):
    # read_chat_rows_async only sees rows Chromium has currently realized (see
    # whatsapp.py's module docstring) — if the list is scrolled, some unread
    # chats further down won't be visible to GetItem yet. So the top-level
    # "Unread" badge count is an upper bound on what we can see here, not
    # necessarily an exact match.
    badge_count = await connector.get_unread_badge_count_async(window_handle)
    unread_rows = await connector.get_unread_chats_async(window_handle)

    assert badge_count >= len(unread_rows)


@pytest.mark.asyncio
async def test_active_conversation_name_is_string_or_none(connector, window_handle):
    name = await connector.get_active_conversation_name_async(window_handle)
    assert name is None or isinstance(name, str)
