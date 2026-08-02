"""App identity: the window/taskbar icon and the Windows AppUserModelID.

Two problems this solves:

- The taskbar showed "Python" with the generic Python icon, because a script
  launched by python.exe inherits that host's identity. Declaring an explicit
  AppUserModelID makes Windows treat winSpark as its own app — its own taskbar
  button, grouping, jump list, and (with the icon set below) its own icon.
- The window had no icon. app_icon() loads winspark.ico/.png from the bundled
  assets, resolving correctly both from source and from a PyInstaller build.

Drop your own winspark.ico (and/or winspark.png) into ui/assets to replace the
placeholder — no code change needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

APP_NAME = "winSpark"
# Any unique string; Windows keys taskbar identity/icon off this.
APP_USER_MODEL_ID = "Anthropic.winSpark.DesktopControlPanel"

_ICON_NAMES = ("winspark.ico", "winspark.png")


def _resource_dir() -> Path:
    """Where bundled data lives — the extracted bundle when frozen by
    PyInstaller, otherwise this package's folder."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "winspark" / "ui" / "assets"
    return Path(__file__).resolve().parent / "assets"


def icon_path() -> Optional[Path]:
    directory = _resource_dir()
    for name in _ICON_NAMES:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def app_icon():
    """A QIcon for the app/window, or an empty one if no icon file is present."""
    from PySide6.QtGui import QIcon

    path = icon_path()
    return QIcon(str(path)) if path is not None else QIcon()


def set_windows_app_identity() -> None:
    """Give the process its own taskbar identity so it shows as winSpark, not
    Python. Must run before any window is created. No-op off Windows."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001 - identity is cosmetic; never block startup
        pass
