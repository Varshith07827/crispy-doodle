"""Models for the WhatsApp connector. Not a 1:1 port of any single .NET file —
these were designed fresh around the GridPattern-based reading technique this
connector uses (see whatsapp.py's module docstring for why)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class WhatsAppChatRow:
    chat_name: str
    timestamp_text: str
    last_message: str
    unread_count: int = 0
    is_pinned: bool = False
    is_muted: bool = False
    is_starred: bool = False
    is_draft: bool = False
    raw_text: str = ""
