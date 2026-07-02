"""Windows-only, live test for WhatsAppGroupSender against real, running
WhatsApp Desktop.

Deliberately does NOT exercise send_to_group_async end-to-end, since that
presses Enter and actually delivers a message to a real contact or group —
not something an automated test should ever do. Instead this verifies every
step up to (but not including) pressing Enter: resolving a real chat by
name via GridPattern, clicking it open, confirming the compose box reflects
the newly active conversation, and typing an obviously-a-test string into it
— then clears the compose box immediately rather than sending. See
PORT_NOTES.md for why send_to_group_async itself is unverified.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires pywin32 + uiautomation on a real Windows session")


@pytest.fixture(scope="module")
def connector(manager):
    from winspark.connectors.whatsapp import WhatsAppConnector

    return WhatsAppConnector(manager)


@pytest.fixture(scope="module")
def sender(connector, manager):
    from winspark.connectors.whatsapp_group_sender import WhatsAppGroupSender

    return WhatsAppGroupSender(connector, manager)


@pytest.fixture(scope="module")
async def first_chat_name(connector):
    hwnd = await connector.find_window_async()
    if hwnd is None:
        pytest.skip("WhatsApp Desktop is not running on this machine")
    rows = await connector.read_chat_rows_async(hwnd)
    if not rows:
        pytest.skip("no chats visible in the chat list")
    return rows[0].chat_name


@pytest.mark.asyncio
async def test_resolve_chat_row_finds_a_real_chat_by_name(sender, first_chat_name):
    window_handle, row = await sender.resolve_chat_row_async(first_chat_name)

    assert window_handle is not None
    assert row is not None
    assert row.chat_name == first_chat_name


@pytest.mark.asyncio
async def test_resolve_chat_row_returns_none_for_a_nonexistent_chat(sender):
    _, row = await sender.resolve_chat_row_async("definitely-not-a-real-chat-xyz-123")
    assert row is None


@pytest.mark.asyncio
async def test_open_chat_and_type_without_sending(sender, connector, first_chat_name):
    """Exercises open-chat + compose-box-typing — everything send_to_group_async
    does except the final Enter keystroke — then clears the draft immediately."""
    from winspark.connectors.whatsapp_group_sender import _compose_is_empty_sync, _open_chat_sync, _set_compose_text_sync

    window_handle, row = await sender.resolve_chat_row_async(first_chat_name)
    assert row is not None

    opened = await sender._sta_manager.invoke_async(lambda: _open_chat_sync(window_handle, row.raw_text))
    assert opened is True

    import asyncio

    await asyncio.sleep(0.3)
    active_name = await connector.get_active_conversation_name_async(window_handle)
    assert active_name is not None

    test_text = "wsport-test-DO-NOT-SEND"
    typed = await sender._sta_manager.invoke_async(lambda: _set_compose_text_sync(window_handle, test_text))
    assert typed is True

    # Verify it actually landed in the compose box, then clear without sending.
    cleared_before_check = await sender._sta_manager.invoke_async(lambda: _compose_is_empty_sync(window_handle))
    assert cleared_before_check is False  # our test text should be there, not empty

    cleared = await sender._sta_manager.invoke_async(lambda: _set_compose_text_sync(window_handle, ""))
    assert cleared is True
    assert await sender._sta_manager.invoke_async(lambda: _compose_is_empty_sync(window_handle)) is True
