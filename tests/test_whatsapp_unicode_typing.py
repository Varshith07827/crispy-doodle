"""Regression test for the emoji-typing bug: uiautomation.SendKeys truncates
any Unicode codepoint above U+FFFF (most emoji) to its low 16 bits before
sending, silently corrupting the text — confirmed live typing "Ma 💖" (U+1F496)
into WhatsApp's search box produced "Ma" (0x1F496 & 0xFFFF), a string
that matched no chat and left WhatsApp's chat list stuck empty.

whatsapp_group_sender._send_unicode_text fixes this by splitting astral
codepoints into a proper UTF-16 surrogate pair before sending each half as its
own Unicode key event — this test verifies that splitting without dispatching
any real keystrokes (SendInput/KeyboardInput are monkeypatched to record calls
instead of actually typing).
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires the uiautomation package (Windows-only)")


@pytest.fixture
def recorder(monkeypatch):
    """Capture the Unicode code units _send_unicode_text would type, instead of
    actually sending keystrokes."""
    from winspark.connectors import whatsapp_group_sender as g

    calls: list[tuple[int, int]] = []  # (wScan, dwFlags)

    def fake_keyboard_input(wVk, wScan, dwFlags=0, time_=0):
        return (wVk, wScan, dwFlags)

    def fake_send_input(*inputs):
        calls.extend((inp[1], inp[2]) for inp in inputs)
        return len(inputs)

    monkeypatch.setattr(g.auto, "KeyboardInput", fake_keyboard_input)
    monkeypatch.setattr(g.auto, "SendInput", fake_send_input)
    return calls


def test_bmp_characters_are_sent_as_a_single_unit(recorder):
    from winspark.connectors.whatsapp_group_sender import _send_unicode_text

    _send_unicode_text("Ma", interval=0)

    units = [scan for scan, _flags in recorder]
    assert units == [ord("M"), ord("M"), ord("a"), ord("a")]  # keydown + keyup per char


def test_astral_character_is_split_into_a_surrogate_pair(recorder):
    from winspark.connectors.whatsapp_group_sender import _send_unicode_text

    _send_unicode_text("\U0001f496", interval=0)  # 💖, U+1F496

    units = [scan for scan, _flags in recorder]
    # The naive (buggy) behavior sends a single unit: ord(char) truncated to 16
    # bits by the OS call — 0x1F496 & 0xFFFF == 0xF496. Assert we do NOT do that.
    assert 0xF496 not in units

    # Correct UTF-16 surrogate pair for U+1F496.
    high, low = 0xD83D, 0xDC96
    assert units == [high, high, low, low]  # keydown + keyup per surrogate half


def test_mixed_text_with_emoji_preserves_order(recorder):
    from winspark.connectors.whatsapp_group_sender import _send_unicode_text

    _send_unicode_text("Ma \U0001f496", interval=0)

    units = [scan for scan, _flags in recorder]
    assert units == [
        ord("M"), ord("M"),
        ord("a"), ord("a"),
        ord(" "), ord(" "),
        0xD83D, 0xD83D,
        0xDC96, 0xDC96,
    ]
