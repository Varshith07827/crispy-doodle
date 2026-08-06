"""Port of WinSpark.Infrastructure.Services.WhatsApp.FetchWebhookResponseParser.

Parses whatever the bound webhook GET returns: plain text, or JSON with the
message under one of a few common field names ("message"/"text"/"content"/
"body"/"msg"), optionally nested under a "data" key, or as an array of
candidate objects.

An array yields EVERY message in it, not just the first. The first is the
result's own message_text (which is what a caller handling one message at a time
reads); the rest follow in `extra_messages`. The port originally kept only the
first and discarded the others silently, so an endpoint that answered a burst
with `[{...},{...},{...}]` delivered one message and dropped two.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

_MESSAGE_FIELD_NAMES = ("message", "text", "content", "body", "msg")


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """One message found in a response, with the id that identifies it for
    dedupe (absent for shapes that carry no id, like plain text)."""

    text: str
    external_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class FetchWebhookParseResult:
    has_message: bool = False
    message_text: Optional[str] = None
    external_id: Optional[str] = None
    parse_strategy: str = ""
    is_error: bool = False
    error_message: Optional[str] = None
    # Messages found in the SAME response after the first. Empty for every shape
    # that can only hold one (plain text, a single object). See all_messages().
    extra_messages: tuple[ParsedMessage, ...] = ()

    @staticmethod
    def blank(strategy: str = "empty") -> "FetchWebhookParseResult":
        return FetchWebhookParseResult(has_message=False, parse_strategy=strategy)

    @staticmethod
    def with_message(text: str, strategy: str, external_id: Optional[str] = None,
                     extra_messages: tuple[ParsedMessage, ...] = ()) -> "FetchWebhookParseResult":
        return FetchWebhookParseResult(has_message=True, message_text=text, parse_strategy=strategy,
                                       external_id=external_id, extra_messages=extra_messages)

    @staticmethod
    def error(message: str) -> "FetchWebhookParseResult":
        return FetchWebhookParseResult(is_error=True, error_message=message)

    def all_messages(self) -> tuple[ParsedMessage, ...]:
        """Every message this response carried, in order — the first plus any
        extras. Empty when there was no message at all."""
        if not self.has_message or not (self.message_text or "").strip():
            return ()
        return (ParsedMessage(self.message_text or "", self.external_id),) + self.extra_messages


def parse(status_code: int, body: Optional[str]) -> FetchWebhookParseResult:
    if status_code == 204:
        return FetchWebhookParseResult.blank("http-204")

    if not body or not body.strip():
        return FetchWebhookParseResult.blank("empty-body" if body is None else "whitespace")

    trimmed = body.strip()

    if trimmed.startswith("{") or trimmed.startswith("["):
        try:
            root = json.loads(trimmed)
        except json.JSONDecodeError as ex:
            return FetchWebhookParseResult.error(f"Invalid JSON: {ex}")
        return _parse_json(root)

    return FetchWebhookParseResult.with_message(trimmed, "plain-text")


def _parse_json(root: object) -> FetchWebhookParseResult:
    if isinstance(root, list):
        # Every item that carries a message, in the order given. Items without
        # one are skipped rather than ending the scan, so a null or a bare
        # heartbeat object between two real messages doesn't hide the second.
        found = [
            parsed for parsed in (_extract_from_object(item, "json-array-item") for item in root)
            if parsed.has_message
        ]
        if not found:
            return FetchWebhookParseResult.blank("json-array-empty")
        first = found[0]
        return FetchWebhookParseResult.with_message(
            first.message_text or "", first.parse_strategy, first.external_id,
            tuple(ParsedMessage(p.message_text or "", p.external_id) for p in found[1:]),
        )

    if not isinstance(root, dict):
        return FetchWebhookParseResult.blank("json-non-object")

    direct = _extract_from_object(root, "json-root")
    if direct.has_message:
        return direct

    data = root.get("data")
    if isinstance(data, dict):
        nested = _extract_from_object(data, "json-data")
        if nested.has_message:
            return nested
    elif isinstance(data, str):
        text = data.strip()
        if text:
            return FetchWebhookParseResult.with_message(text, "json-data-string")

    return FetchWebhookParseResult.blank("json-no-message")


def _extract_from_object(obj: object, strategy_prefix: str) -> FetchWebhookParseResult:
    if not isinstance(obj, dict):
        return FetchWebhookParseResult.blank(f"{strategy_prefix}:empty-fields")

    external_id = obj.get("id") if isinstance(obj.get("id"), str) else None

    for field_name in _MESSAGE_FIELD_NAMES:
        if field_name not in obj:
            continue
        text = _read_text_value(obj[field_name])
        if text:
            return FetchWebhookParseResult.with_message(text, f"{strategy_prefix}:{field_name}", external_id)

    return FetchWebhookParseResult.blank(f"{strategy_prefix}:empty-fields")


def _read_text_value(value: object) -> Optional[str]:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return None
