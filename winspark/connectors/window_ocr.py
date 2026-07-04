"""Read the text on any app's window using the OCR engine built into Windows.

This is winSpark's answer to reading apps it has no accessibility adapter for:
capture the window's pixels (PrintWindow, which also grabs DirectComposition/
Chromium content), hand them to Windows.Media.Ocr, and get back the visible
text. No Tesseract binary and no cloud service — the engine ships with Windows.

Everything degrades gracefully: if the WinRT OCR packages or pywin32/Pillow
aren't installed, is_available() is False and read_window_text returns a plain
message telling the user what to install, rather than raising.
"""

from __future__ import annotations

import asyncio
from ctypes import windll
from dataclasses import dataclass
from typing import Optional

try:
    import win32con
    import win32gui
    import win32ui
    from PIL import Image, ImageStat

    _CAPTURE_AVAILABLE = True
except ImportError:  # pragma: no cover - off-Windows / missing deps
    _CAPTURE_AVAILABLE = False

try:
    # winrt.windows.foundation is imported explicitly (not used directly) because
    # the OCR async call pulls it in lazily at runtime — importing it here means
    # a partial install is caught by is_available() instead of crashing mid-OCR.
    import winrt.windows.foundation  # noqa: F401
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    _OCR_AVAILABLE = True
except ImportError:  # pragma: no cover - winrt packages not installed
    _OCR_AVAILABLE = False

# PW_RENDERFULLCONTENT — makes PrintWindow capture GPU/DirectComposition content
# (needed for Chromium/WebView2 apps), not just the classic GDI surface.
_PW_RENDERFULLCONTENT = 2

# Below this per-pixel brightness spread, a capture is effectively uniform —
# i.e. PrintWindow handed back a black/blank frame instead of the window's
# pixels. Measured: failing captures come back at ~0.0 stddev; real windows,
# even dark-themed ones, sit far above this.
_BLANK_STDDEV = 3.0

_INSTALL_HINT = (
    "Reading text on screen needs the Windows OCR packages. Install them with:\n"
    "  pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging "
    "winrt-Windows.Storage.Streams winrt-Windows.Globalization winrt-Windows.Foundation"
)


@dataclass(frozen=True, slots=True)
class OcrResult:
    ok: bool = False
    text: str = ""
    error: str = ""

    @staticmethod
    def succeeded(text: str) -> "OcrResult":
        return OcrResult(ok=True, text=text)

    @staticmethod
    def failed(error: str) -> "OcrResult":
        return OcrResult(ok=False, error=error)


def is_available() -> bool:
    return _CAPTURE_AVAILABLE and _OCR_AVAILABLE


def capture_window_png(window_handle: int) -> Optional[bytes]:
    """Capture the window as PNG bytes (for showing the user exactly what was
    read), or None if capture isn't possible. Doesn't need the OCR packages —
    only pywin32 + Pillow."""
    if not _CAPTURE_AVAILABLE:
        return None
    try:
        image = _capture_window(window_handle)
        if image is None:
            return None
        import io

        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a failed preview must not break the OCR flow
        return None


def read_window_text(window_handle: int) -> OcrResult:
    """Capture the given window and OCR it. Synchronous; safe to call from a UI
    thread (takes well under a second for a typical window)."""
    if not _CAPTURE_AVAILABLE:
        return OcrResult.failed("Reading the screen needs pywin32 and Pillow, which aren't installed.")
    if not _OCR_AVAILABLE:
        return OcrResult.failed(_INSTALL_HINT)

    try:
        image = _capture_window(window_handle)
    except Exception as ex:  # noqa: BLE001
        return OcrResult.failed(f"Couldn't capture that window — {ex}")
    if image is None:
        return OcrResult.failed("That window can't be read right now — try bringing it to the front first.")

    try:
        text = _run_ocr(image)
    except ModuleNotFoundError:  # a winrt subpackage is missing at call time
        return OcrResult.failed(_INSTALL_HINT)
    except Exception as ex:  # noqa: BLE001
        return OcrResult.failed(f"OCR failed — {ex}")

    # A window can capture fine yet still OCR to nothing when its body is
    # composited in a way PrintWindow can't see (a UWP app whose title bar
    # renders but whose content doesn't — not blank enough to trip the capture
    # fallback, but empty of text). If it's visible and unoccluded, a
    # screen-framebuffer grab sees the real content; try that before giving up.
    if (text is None or not text.strip()) and _is_unoccluded(window_handle):
        shot = _bitblt_window(window_handle)
        if shot is not None:
            try:
                alt = _run_ocr(shot)
            except Exception:  # noqa: BLE001
                alt = None
            if alt and alt.strip():
                text = alt

    if text is None:
        return OcrResult.failed("Windows OCR isn't set up — add an OCR language pack in Windows settings.")
    if not text.strip():
        if win32gui.IsIconic(window_handle):
            return OcrResult.failed("That window is minimized — restore it and try again.")
        return OcrResult.failed(
            "Couldn't read any text from that window. Some Store and system apps render in a way "
            "we can't capture while they're in the background — bring the window to the front and try again."
        )
    return OcrResult.succeeded(text)


def _capture_window(window_handle: int):
    """Best-effort capture of a window's pixels.

    PrintWindow is the primary path (works for classic + Chromium/WebView2
    windows and doesn't need the window in front). But it hands back a black
    frame for UWP apps (Settings, Store apps) and some GPU-composited windows,
    whose content isn't rendered into the DC it reads. When that happens AND the
    window is actually visible with nothing over it, the screen's own
    framebuffer holds the real pixels — so grab those instead. The occlusion
    check is what keeps this honest: we never BitBlt a window that something
    else is sitting on top of (we'd capture the wrong thing)."""
    image = _print_window(window_handle)
    if image is not None and not _looks_blank(image):
        return image
    if _is_unoccluded(window_handle):
        shot = _bitblt_window(window_handle)
        if shot is not None and not _looks_blank(shot):
            return shot
    return image


def _print_window(window_handle: int):
    left, top, right, bottom = win32gui.GetWindowRect(window_handle)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    window_dc = win32gui.GetWindowDC(window_handle)
    mfc_dc = win32ui.CreateDCFromHandle(window_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        windll.user32.PrintWindow(window_handle, save_dc.GetSafeHdc(), _PW_RENDERFULLCONTENT)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(window_handle, window_dc)


def _bitblt_window(window_handle: int):
    """Copy the window's screen region straight out of the display framebuffer.
    Sees whatever is actually painted there — including UWP/GPU content — but
    only correct when the region isn't covered by another window (see the
    caller's occlusion gate)."""
    left, top, right, bottom = win32gui.GetWindowRect(window_handle)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    screen_dc = win32gui.GetDC(0)
    mfc_dc = win32ui.CreateDCFromHandle(screen_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (left, top), win32con.SRCCOPY)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(0, screen_dc)


def _looks_blank(image) -> bool:
    """True if the capture is effectively one flat colour — the signature of a
    failed PrintWindow (black frame)."""
    if image is None:
        return True
    try:
        return ImageStat.Stat(image.convert("L")).stddev[0] < _BLANK_STDDEV
    except Exception:  # noqa: BLE001 - a stats hiccup shouldn't block capture
        return False


def _is_unoccluded(window_handle: int) -> bool:
    """True if the window's centre point actually belongs to this window on
    screen — i.e. nothing else is covering it there. Guards the screen-grab
    fallback so it can't capture an overlapping window instead."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(window_handle)
        center = ((left + right) // 2, (top + bottom) // 2)
        hit = win32gui.WindowFromPoint(center)
        if not hit:
            return False
        return win32gui.GetAncestor(hit, win32con.GA_ROOT) == window_handle
    except Exception:  # noqa: BLE001
        return False


def _to_software_bitmap(image):
    rgba = image.convert("RGBA")
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes("raw", "BGRA"))
    buffer = writer.detach_buffer()
    return SoftwareBitmap.create_copy_from_buffer(buffer, BitmapPixelFormat.BGRA8, rgba.width, rgba.height)


def _run_ocr(image):
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        return None
    bitmap = _to_software_bitmap(image)

    # recognize_async is a WinRT IAsyncOperation; run it to completion on a
    # private event loop so this stays a simple synchronous call for callers.
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(engine.recognize_async(bitmap))
    finally:
        loop.close()
    return result.text
