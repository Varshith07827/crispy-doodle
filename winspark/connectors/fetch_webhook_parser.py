"""Port of WinSpark.Infrastructure.Services.WhatsApp.FetchWebhookResponseParser.

Parses whatever the bound webhook GET returns: plain text, or JSON with the
message under one of a few common field names ("message"/"text"/"content"/
"body"/"msg"), optionally nested under a "data" key, or as an array of
candidate objects (first one with a message wins).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

_MESSAGE_FIELD_NAMES = ("message", "text", "content", "body", "msg")


@dataclass(frozen=True, slots=True)
class FetchWebhookParseResult:
    has_message: bool = False
    message_text: Optional[str] = None
    external_id: Optional[str] = None
    parse_strategy: str = ""
    is_error: bool = False
    error_message: Optional[str] = None

    @staticmethod
    def blank(strategy: str = "empty") -> "FetchWebhookParseResult":
        return FetchWebhookParseResult(has_message=False, parse_strategy=strategy)

    @staticmethod
    def with_message(text: str, strategy: str, external_id: Optional[str] = None) -> "FetchWebhookParseResult":
        return FetchWebhookParseResult(has_message=True, message_text=text, parse_strategy=strategy, external_id=external_id)

    @staticmethod
    def error(message: str) -> "FetchWebhookParseResult":
        return FetchWebhookParseResult(is_error=True, error_message=message)


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
        for item in root:
            from_item = _extract_from_object(item, "json-array-item")
            if from_item.has_message:
                return from_item
        return FetchWebhookParseResult.blank("json-array-empty")

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
