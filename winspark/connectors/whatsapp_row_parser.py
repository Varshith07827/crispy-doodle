"""Parses a WhatsApp Desktop chat-list row's UI Automation accessible Name
into structured fields.

Each row is a single concatenated string (Chromium accessibility flattens all
descendant text into the row's Name), e.g.:

    "4 unread messages Vishnu Cr Gvp Yesterday ekada grp names navi unaye..."
    "CSE - C Yesterday Chaitu: https://chat.whatsapp.com/... Pinned chat"

There's no delimiter between chat name / timestamp / message — this is a
heuristic parser (leading "N unread messages" prefix, trailing flag phrases,
then a day/time "anchor" splits name from message), not a formal grammar. It
was written and tuned against 5 real rows captured live from a running
WhatsApp Desktop instance (see test_whatsapp_row_parser.py) rather than
guessed at, but it will misparse chat names that happen to contain a day
name or a time-like substring.
"""

from __future__ import annotations

import re

# WhatsApp labels a chat row's avatar "View status" when that contact has an
# (unseen) status posted, and Chromium flattens that button's name into the row
# ahead of everything else — so a real row reads "View status 2 unread messages
# Hasini …". Strip it (before the unread prefix) or it becomes part of the name.
_STATUS_PREFIX = re.compile(r"^View status\b[\s,]*", re.IGNORECASE)

_UNREAD_PREFIX = re.compile(r"^(\d+)\s+unread messages?\s+")

_TRAILING_FLAGS: tuple[tuple[str, str], ...] = (
    (" Starred chat", "is_starred"),
    (" Pinned chat", "is_pinned"),
    (" Muted chat", "is_muted"),
    (" Draft message", "is_draft"),
)

_ANCHOR = re.compile(
    r"\b(?:Yesterday|Today|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
    r"|\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?|\d{1,2}/\d{1,2}/\d{2,4})\b"
)


def parse_chat_row(raw_text: str):
    """Returns a dict of parsed fields; caller wraps into WhatsAppChatRow.
    Kept as a plain dict here so this module has no dependency on the
    dataclass module (easy to unit test standalone)."""
    text = raw_text.strip()

    # Drop the avatar "View status" button prefix first, so the unread-count
    # prefix and the real chat name are reachable.
    text = _STATUS_PREFIX.sub("", text, count=1)

    unread_count = 0
    match = _UNREAD_PREFIX.match(text)
    if match:
        unread_count = int(match.group(1))
        text = text[match.end():]

    flags = {"is_pinned": False, "is_muted": False, "is_starred": False, "is_draft": False}
    changed = True
    while changed:
        changed = False
        for phrase, flag_name in _TRAILING_FLAGS:
            if text.endswith(phrase):
                text = text[: -len(phrase)]
                flags[flag_name] = True
                changed = True

    anchor_match = _ANCHOR.search(text)
    if anchor_match:
        chat_name = text[: anchor_match.start()].strip()
        timestamp_text = anchor_match.group(0)
        last_message = text[anchor_match.end():].strip()
    else:
        chat_name = text.strip()
        timestamp_text = ""
        last_message = ""

    return {
        "chat_name": chat_name,
        "timestamp_text": timestamp_text,
        "last_message": last_message,
        "unread_count": unread_count,
        "raw_text": raw_text,
        **flags,
    }
