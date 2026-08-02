"""The shared action lock serialises whole real-input operations, so a second
send / chat-open can't change the open chat mid-send — the fix for "my message
went to the chat that was being opened instead of mine"."""

import asyncio

import pytest

from winspark.connectors.whatsapp_group_sender import WhatsAppGroupSender


class _Sta:
    """STA stand-in with the real action_lock and an inline invoke_async."""

    def __init__(self):
        self.action_lock = asyncio.Lock()

    async def invoke_async(self, fn):
        return fn()


class _Row:
    raw_text = "Family 1:00 pm hi"


def _connector():
    async def active_name(_handle):
        return "Family"
    return type("C", (), {"get_active_conversation_name_async": staticmethod(active_name)})()


def _sender(sta, tag, order):
    sender = WhatsAppGroupSender(connector=_connector(), sta_manager=sta)

    async def resolve(_group):
        order.append(f"{tag}:enter")
        await asyncio.sleep(0.01)  # yield: an unlocked impl would let the other in here
        order.append(f"{tag}:exit")
        return 4242, _Row()

    sender.resolve_chat_row_async = resolve
    return sender


@pytest.fixture
def _stub_uia(monkeypatch):
    import winspark.connectors.whatsapp_group_sender as gs
    monkeypatch.setattr(gs, "_open_chat_sync", lambda *a, **k: True)
    monkeypatch.setattr(gs, "_set_compose_text_sync", lambda *a, **k: True)
    monkeypatch.setattr(gs, "_invoke_send_button_sync", lambda *a, **k: True)
    monkeypatch.setattr(gs, "_compose_is_empty_sync", lambda *a, **k: True)


@pytest.mark.asyncio
async def test_two_concurrent_sends_do_not_interleave(_stub_uia):
    sta = _Sta()
    order: list[str] = []
    a = _sender(sta, "A", order)
    b = _sender(sta, "B", order)

    await asyncio.gather(
        a.send_to_group_async("Family", "from A"),
        b.send_to_group_async("Work", "from B"),
    )

    # Whichever send ran first, its enter/exit must be adjacent — the other send
    # could NOT slip in during the awaited yield inside resolve. Without the lock
    # the order would be A:enter, B:enter, A:exit, B:exit.
    assert order in (
        ["A:enter", "A:exit", "B:enter", "B:exit"],
        ["B:enter", "B:exit", "A:enter", "A:exit"],
    )


@pytest.mark.asyncio
async def test_open_chat_waits_for_an_in_flight_send(_stub_uia):
    sta = _Sta()
    order: list[str] = []
    sender = _sender(sta, "send", order)

    async def competing_open():
        # Models open_chat_async's body: it too acquires the action lock.
        async with sta.action_lock:
            order.append("open:done")

    send_task = asyncio.create_task(sender.send_to_group_async("Family", "x"))
    await asyncio.sleep(0)                    # let the send grab the lock first
    open_task = asyncio.create_task(competing_open())
    await asyncio.gather(send_task, open_task)

    # The open only completed after the whole send finished.
    assert order[-1] == "open:done"
    assert order.index("send:exit") < order.index("open:done")
