"""Deciding whether an incoming message matches a "wait for" trigger phrase.

Two strategies: a semantic match via OpenAI (used when a key is configured — it
understands meaning, e.g. "are you coming?" matches the trigger "asking if I'll
show up"), and this literal fallback that matches by words when OpenAI isn't
available. The literal matcher is deliberately lenient: a full-substring hit, or
all of the trigger's significant words appearing somewhere in the message.

`command_match` is the opposite kind of match and exists for the opposite
reason — see its docstring.
"""

from __future__ import annotations

import re

from winspark.constants import AI_COMMAND_PREFIX

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MIN_SIGNIFICANT_WORD_LEN = 3

# Words people put in FRONT of the command ("hey !winspark …"). They're
# addressing noise, not part of the question, and only matter when the command
# is written last ("what's the weather !winspark") — see command_match.
_FILLER_WORDS = frozenset({
    "hey", "hi", "hello", "yo", "ok", "okay", "so", "um", "hmm", "please", "pls",
})

# Punctuation that separates the command from the question: "!winspark, what's up".
_QUERY_EDGE = " \t\r\n,:;.-–—?!"


def _strip_filler(text: str) -> str:
    words = text.split()
    while words and words[0].strip(_QUERY_EDGE).lower() in _FILLER_WORDS:
        words.pop(0)
    return " ".join(words)


def command_match(command_word: str, message: str,
                  prefix: str = AI_COMMAND_PREFIX) -> tuple[bool, str]:
    """Is this message addressed to the bot by name, and if so what's being asked?

    Returns ``(matched, query)`` — e.g. ``("!winspark what's the weather")`` ->
    ``(True, "what's the weather")``.

    Unlike `literal_match` above, this is deliberately STRICT. `literal_match`
    answers "is this message roughly about the trigger?", which is right for a
    watch-for-a-phrase rule but catastrophic for a bot name: it would fire on
    "I was reading about winspark yesterday", barging into a conversation that
    never addressed it. Being addressed is a yes/no fact about the text, so it
    is matched exactly — no AI call, no fuzziness, no cost per message.

    Two rules do the real work:

    * The prefix must start the message or follow whitespace. Without that,
      ``docs/winspark/readme`` and ``http://x.com/winspark`` would both trigger
      the bot — the reason the prefix is worth having at all.
    * The name must not run straight into more letters, so ``!winsparkling`` is
      not a match. This is ``(?!\\w)`` rather than ``\\b`` because a name can end
      in punctuation — after "c++" there is no word boundary for ``\\b`` to find,
      and the bot would never answer to its own name.
    """
    word = (command_word or "").strip().lstrip("!@/#").strip()
    text = (message or "").strip()
    if not word or not text:
        return False, ""

    pattern = re.compile(
        r"(?:^|\s)" + re.escape(prefix) + re.escape(word) + r"(?!\w)",
        re.IGNORECASE | re.UNICODE,
    )
    found = pattern.search(text)
    if found is None:
        return False, ""

    # The question normally follows the command. When it doesn't — someone
    # wrote "what's the weather !winspark" — fall back to what came before,
    # minus the greeting words that were addressing the bot rather than asking.
    after = text[found.end():].strip().lstrip(_QUERY_EDGE).strip()
    if after:
        return True, after
    return True, _strip_filler(text[:found.start()].strip())


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
