"""Ports of WinSpark.Domain.Models.* (WindowInfo, WindowDiscoverySnapshot, BusEvent)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from winspark.domain.enums import BusEventCategory, WindowStateKind


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class WindowInfo:
    handle: int
    title: str = ""
    process_name: str = ""
    friendly_name: str = ""
    process_id: int = 0
    executable_path: str = ""
    memory_bytes: int = 0
    cpu_percent: float = 0.0
    start_time_utc: Optional[datetime] = None
    window_state: WindowStateKind = WindowStateKind.NORMAL
    is_active: bool = False
    # Explicit per-window app identity (AppUserModelID). Installed web apps
    # (Chrome/Edge PWAs) carry a distinct "..._crx_..." id — how Task View
    # knows YouTube-the-app isn't just another browser window.
    app_user_model_id: str = ""

    @property
    def memory_display(self) -> str:
        return self.format_bytes(self.memory_bytes)

    @staticmethod
    def format_bytes(num_bytes: int) -> str:
        if num_bytes < 1024:
            return f"{num_bytes} B"
        if num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024:.1f} KB"
        if num_bytes < 1024**3:
            return f"{num_bytes / (1024 * 1024):.1f} MB"
        return f"{num_bytes / (1024**3):.1f} GB"


@dataclass(frozen=True, slots=True)
class WindowDiscoverySnapshot:
    windows: tuple[WindowInfo, ...] = ()
    active_window: Optional[WindowInfo] = None
    captured_at_utc: datetime = field(default_factory=_utcnow)
    discovery_duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class BusEvent:
    category: BusEventCategory
    event_type: str
    payload: Any = None
    source: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at_utc: datetime = field(default_factory=_utcnow)
