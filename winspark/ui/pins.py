"""Pinned apps — the sidebar entries that survive the app being closed.

Stored in ``data.json`` next to the database, not in SQLite: it's a small,
user-owned list a person might reasonably hand-edit or copy between machines.
Writes are atomic (temp file + ``os.replace``) so a crash mid-write leaves the
previous file intact, and any unreadable file degrades to "no pins" — losing
pins must never be worse than starting fresh.

A pin remembers the app's executable path so clicking a pinned-but-closed app
can launch it. WhatsApp is not stored here: it is pinned by design, always
first in the sidebar, and launches via its ``whatsapp:`` URL scheme rather
than an executable path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PinnedApp:
    name: str            # what the sidebar shows, e.g. "VS Code"
    process: str         # lowercase process name, e.g. "code.exe" — the identity
    path: str = ""       # executable to launch when the app is closed

    @staticmethod
    def from_dict(raw: dict) -> Optional["PinnedApp"]:
        name = str(raw.get("name") or "").strip()
        process = str(raw.get("process") or "").strip().lower()
        if not name or not process:
            return None
        return PinnedApp(name=name, process=process, path=str(raw.get("path") or "").strip())


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        logger.warning("%s is unreadable (%s) — starting with no pins", path.name, ex)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class PinStore:
    """The pinned-apps list, kept in memory and persisted on every change."""

    def __init__(self, directory: Path) -> None:
        self._path = Path(directory) / "data.json"
        self._lock = threading.Lock()
        raw = _read_json(self._path).get("pinned_apps")
        rows = (PinnedApp.from_dict(r) for r in raw if isinstance(r, dict)) \
            if isinstance(raw, list) else ()
        self._pins: tuple[PinnedApp, ...] = tuple(p for p in rows if p is not None)

    @property
    def path(self) -> Path:
        return self._path

    def pins(self) -> tuple[PinnedApp, ...]:
        with self._lock:
            return self._pins

    def is_pinned(self, process: str) -> bool:
        wanted = (process or "").strip().lower()
        return any(p.process == wanted for p in self.pins())

    def pin(self, name: str, process: str, path: str = "") -> None:
        name = (name or "").strip()
        process = (process or "").strip().lower()
        if not name or not process:
            return
        with self._lock:
            others = tuple(p for p in self._pins if p.process != process)
            self._pins = others + (PinnedApp(name=name, process=process, path=path or ""),)
            self._save()

    def unpin(self, process: str) -> None:
        wanted = (process or "").strip().lower()
        with self._lock:
            kept = tuple(p for p in self._pins if p.process != wanted)
            if len(kept) != len(self._pins):
                self._pins = kept
                self._save()

    def _save(self) -> None:
        # Merge into whatever else data.json holds, so a future key added by
        # another feature isn't wiped by a pin change.
        payload = _read_json(self._path)
        payload["pinned_apps"] = [asdict(p) for p in self._pins]
        _write_json(self._path, payload)
