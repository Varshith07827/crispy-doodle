"""Tests FetchWebhookResponseParser port: plain text, JSON field extraction
(message/text/content/body/msg), nested "data", arrays, and error handling.
Pure logic — runs on any platform.
"""

from winspark.connectors.fetch_webhook_parser import parse


def test_http_204_is_blank():
    result = parse(204, "should be ignored")
    assert result.has_message is False
    assert result.parse_strategy == "http-204"


def test_empty_body_is_blank():
    assert parse(200, "").parse_strategy == "whitespace"
    assert parse(200, None).parse_strategy == "empty-body"
    assert parse(200, "   ").parse_strategy == "whitespace"


def test_plain_text_response():
    result = parse(200, "Hello from the AI")
    assert result.has_message is True
    assert result.message_text == "Hello from the AI"
    assert result.parse_strategy == "plain-text"


def test_plain_text_is_trimmed():
    result = parse(200, "  padded text  ")
    assert result.message_text == "padded text"


def test_json_object_with_message_field():
    result = parse(200, '{"message": "hi there"}')
    assert result.has_message is True
    assert result.message_text == "hi there"
    assert result.parse_strategy == "json-root:message"


def test_json_object_field_priority_and_id():
    result = parse(200, '{"id": "abc123", "text": "second field wins if message missing"}')
    assert result.message_text == "second field wins if message missing"
    assert result.external_id == "abc123"
    assert result.parse_strategy == "json-root:text"


def test_json_nested_under_data_object():
    result = parse(200, '{"data": {"body": "nested message"}}')
    assert result.has_message is True
    assert result.message_text == "nested message"
    assert result.parse_strategy == "json-data:body"


def test_json_data_as_plain_string():
    result = parse(200, '{"data": "just a string"}')
    assert result.has_message is True
    assert result.message_text == "just a string"
    assert result.parse_strategy == "json-data-string"


def test_json_array_returns_first_item_with_message():
    result = parse(200, '[{"content": ""}, {"msg": "found it"}]')
    assert result.has_message is True
    assert result.message_text == "found it"
    assert result.parse_strategy == "json-array-item:msg"


def test_json_empty_array_is_blank():
    result = parse(200, "[]")
    assert result.has_message is False
    assert result.parse_strategy == "json-array-empty"


def test_json_object_with_no_recognized_field_is_blank():
    # Matches the C# original: ParseJson falls through past the failed root
    # extraction to check "data", and having neither, reports "json-no-message"
    # rather than the root extractor's own "json-root:empty-fields" strategy.
    result = parse(200, '{"unrelated": "value"}')
    assert result.has_message is False
    assert result.parse_strategy == "json-no-message"


def test_invalid_json_is_an_error():
    result = parse(200, "{not valid json")
    assert result.is_error is True
    assert "Invalid JSON" in result.error_message


def test_numeric_and_boolean_field_values_are_stringified():
    assert parse(200, '{"message": 42}').message_text == "42"
    assert parse(200, '{"message": true}').message_text == "true"


def test_whitespace_only_field_value_does_not_count_as_a_message():
    result = parse(200, '{"message": "   "}')
    assert result.has_message is False
