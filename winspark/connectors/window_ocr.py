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

try:
    import win32gui
    import win32ui
    from PIL import Image

    _CAPTURE_AVAILABLE = True
except ImportError:  # pragma: no cover - off-Windows / missing deps
    _CAPTURE_AVAILABLE = False

try:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    _OCR_AVAILABLE = True
except ImportError:  # pragma: no cover - winrt packages not installed
    _OCR_AVAILABLE = False

# PW_RENDERFULLCONTENT — makes PrintWindow capture GPU/DirectComposition content
# (needed for Chromium/WebView2 apps), not just the classic GDI surface.
_PW_RENDERFULLCONTENT = 2

_INSTALL_HINT = (
    "Reading text on screen needs the Windows OCR packages. Install them with:\n"
    "  pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging "
    "winrt-Windows.Storage.Streams winrt-Windows.Globalization"
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
    except Exception as ex:  # noqa: BLE001
        return OcrResult.failed(f"OCR failed — {ex}")

    if text is None:
        return OcrResult.failed("Windows OCR isn't set up — add an OCR language pack in Windows settings.")
    if not text.strip():
        return OcrResult.failed("No readable text was found on that window.")
    return OcrResult.succeeded(text)


def _capture_window(window_handle: int):
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
