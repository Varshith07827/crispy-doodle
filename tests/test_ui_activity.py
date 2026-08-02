"""Tests the plain-English activity translation (pure, cross-platform)."""

from winspark.ui.activity import describe_activity, friendly_reason


def test_automation_on_off():
    assert "Automation started" in describe_activity("", "automation_on")
    assert describe_activity("", "automation_off") == "Automation stopped"


def test_checking():
    assert describe_activity("Family", "checking") == "Checking for a new message…"


def test_received_sending_sent_include_chat_name():
    assert describe_activity("Family", "received") == "New message received for Family"
    assert describe_activity("Family", "sending") == "Sending the message to Family…"
    assert describe_activity("Family", "sent") == "Sent the message to Family"


def test_no_technical_terms_leak_for_common_failures():
    src = describe_activity("Family", "source_error", "HTTP 500: Internal Server Error")
    assert "HTTP" not in src
    assert "returned an error" in src

    fail = describe_activity("Family", "send_failed", "WhatsApp is not running")
    assert "Couldn't send the message to Family" in fail
    assert "isn't open" in fail


def test_friendly_reason_maps_common_cases():
    assert friendly_reason("HTTP 404 Not Found") == "the website rejected the request"
    assert friendly_reason("Chat 'X' not found in the visible chat list.") == "the chat couldn't be found"
    assert friendly_reason("WhatsApp is not in the foreground") == "the app wasn't in front"
    assert friendly_reason("") == ""


def test_unknown_kind_is_humanized_not_crashing():
    out = describe_activity("Family", "some_new_kind")
    assert "Some new kind" in out
