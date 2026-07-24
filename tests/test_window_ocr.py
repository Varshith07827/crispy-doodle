"""Tests for the screen-capture robustness added to window_ocr: blank-frame
detection, the screen-grab fallback that recovers windows PrintWindow renders
black (UWP/GPU apps), the occlusion gate that keeps that fallback honest, and
the plain-English failure messages. The Windows-specific capture/OCR calls are
stubbed, so these run anywhere."""

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from winspark.connectors import window_ocr  # noqa: E402


def _solid(color):
    return Image.new("RGB", (120, 60), color)


def _noisy():
    img = Image.new("L", (120, 60))
    img.putdata([(i * 37) % 256 for i in range(120 * 60)])
    return img.convert("RGB")


# --- blank detection ---------------------------------------------------------

def test_looks_blank_true_for_black_and_none():
    assert window_ocr._looks_blank(_solid((0, 0, 0))) is True
    assert window_ocr._looks_blank(_solid((255, 255, 255))) is True  # any flat colour
    assert window_ocr._looks_blank(None) is True


def test_looks_blank_false_for_real_content():
    assert window_ocr._looks_blank(_noisy()) is False


# --- read_window_text fallback wiring ---------------------------------------

@pytest.fixture
def available(monkeypatch):
    monkeypatch.setattr(window_ocr, "_CAPTURE_AVAILABLE", True)
    monkeypatch.setattr(window_ocr, "_OCR_AVAILABLE", True)
    monkeypatch.setattr(window_ocr.win32gui, "IsIconic", lambda h: False)


def test_screen_grab_recovers_a_window_printwindow_reads_empty(available, monkeypatch):
    # PrintWindow captures something, but OCR finds no text (the UWP
    # content-missing case). The window is unoccluded, so the framebuffer grab
    # is consulted and it has the real text.
    monkeypatch.setattr(window_ocr, "_capture_window", lambda h: "PRINT")
    monkeypatch.setattr(window_ocr, "_is_unoccluded", lambda h: True)
    monkeypatch.setattr(window_ocr, "_bitblt_window", lambda h: "SHOT")
    monkeypatch.setattr(window_ocr, "_run_ocr", lambda img: "" if img == "PRINT" else "Settings System")

    result = window_ocr.read_window_text(1234)
    assert result.ok is True
    assert result.text == "Settings System"


def test_no_screen_grab_when_occluded_gives_honest_error(available, monkeypatch):
    grabbed = []
    monkeypatch.setattr(window_ocr, "_capture_window", lambda h: "PRINT")
    monkeypatch.setattr(window_ocr, "_is_unoccluded", lambda h: False)  # something's on top
    monkeypatch.setattr(window_ocr, "_bitblt_window", lambda h: grabbed.append(h) or "SHOT")
    monkeypatch.setattr(window_ocr, "_run_ocr", lambda img: "")

    result = window_ocr.read_window_text(1234)
    assert grabbed == []  # never captured the covering window
    assert result.ok is False
    assert "front" in result.error and "minimized" not in result.error


def test_minimized_window_says_so(available, monkeypatch):
    monkeypatch.setattr(window_ocr, "_capture_window", lambda h: "PRINT")
    monkeypatch.setattr(window_ocr, "_is_unoccluded", lambda h: False)
    monkeypatch.setattr(window_ocr, "_run_ocr", lambda img: "")
    monkeypatch.setattr(window_ocr.win32gui, "IsIconic", lambda h: True)

    result = window_ocr.read_window_text(1234)
    assert result.ok is False
    assert "minimized" in result.error


def test_normal_capture_needs_no_fallback(available, monkeypatch):
    calls = []
    monkeypatch.setattr(window_ocr, "_capture_window", lambda h: "PRINT")
    monkeypatch.setattr(window_ocr, "_run_ocr", lambda img: "plenty of real text here")
    monkeypatch.setattr(window_ocr, "_is_unoccluded", lambda h: calls.append(h) or True)

    result = window_ocr.read_window_text(1234)
    assert result.ok is True
    assert calls == []  # the fallback path was never even considered


# --- media thumbnail crop ----------------------------------------------------

def test_crop_window_region_translates_screen_rect_to_local(monkeypatch):
    # A 300x200 window captured whole; crop a screen rect that maps to a
    # 100x80 region inside it. Verifies the screen->window-local translation.
    captured = Image.new("RGB", (300, 200), (10, 20, 30))
    monkeypatch.setattr(window_ocr, "_CAPTURE_AVAILABLE", True)
    monkeypatch.setattr(window_ocr, "_capture_window", lambda h: captured)
    monkeypatch.setattr(window_ocr.win32gui, "GetWindowRect", lambda h: (1000, 500, 1300, 700))

    png = window_ocr.crop_window_region_png(1, (1050, 560, 1150, 640))
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
    import io
    assert Image.open(io.BytesIO(png)).size == (100, 80)


def test_crop_returns_none_when_region_misses_window(monkeypatch):
    captured = Image.new("RGB", (300, 200), (0, 0, 0))
    monkeypatch.setattr(window_ocr, "_CAPTURE_AVAILABLE", True)
    monkeypatch.setattr(window_ocr, "_capture_window", lambda h: captured)
    monkeypatch.setattr(window_ocr.win32gui, "GetWindowRect", lambda h: (1000, 500, 1300, 700))
    # A rect entirely left of the window -> no overlap -> None.
    assert window_ocr.crop_window_region_png(1, (0, 0, 5, 5)) is None


def test_crop_returns_none_when_capture_unavailable(monkeypatch):
    monkeypatch.setattr(window_ocr, "_CAPTURE_AVAILABLE", False)
    assert window_ocr.crop_window_region_png(1, (0, 0, 10, 10)) is None
