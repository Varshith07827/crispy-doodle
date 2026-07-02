"""Deciding whether an incoming message matches a "wait for" trigger phrase.

Two strategies: a semantic match via OpenAI (used when a key is configured — it
understands meaning, e.g. "are you coming?" matches the trigger "asking if I'll
show up"), and this literal fallback that matches by words when OpenAI isn't
available. The literal matcher is deliberately lenient: a full-substring hit, or
all of the trigger's significant words appearing somewhere in the message.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MIN_SIGNIFICANT_WORD_LEN = 3


def literal_match(trigger: str, message: str) -> bool:
    trigger_norm = (trigger or "").strip().lower()
    message_norm = (message or "").strip().lower()
    if not trigger_norm or not message_norm:
        return False
    if trigger_norm in message_norm:
        return True

    trigger_words = [w for w in _WORD_RE.findall(trigger_norm) if len(w) >= _MIN_SIGNIFICANT_WORD_LEN]
    if not trigger_words:
        # trigger is only short/stop-ish words — fall back to any-word overlap
        trigger_words = _WORD_RE.findall(trigger_norm)
        if not trigger_words:
            return False
        message_words = set(_WORD_RE.findall(message_norm))
        return any(w in message_words for w in trigger_words)

    message_words = set(_WORD_RE.findall(message_norm))
    return all(w in message_words for w in trigger_words)
