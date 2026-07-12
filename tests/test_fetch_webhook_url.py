"""Tests FetchWebhookUrlNormalizer port. Pure logic — runs on any platform."""

from winspark.connectors.fetch_webhook_url import normalize_poll_url, try_validate_poll_url


def test_empty_raw_defaults_to_local_mock_url():
    assert normalize_poll_url(None, "Infosys") == "http://127.0.0.1:5001/webhook/Infosys"
    assert normalize_poll_url("", "Infosys") == "http://127.0.0.1:5001/webhook/Infosys"


def test_strips_leading_get_or_post_verb():
    assert normalize_poll_url("GET http://example.com/hook", "g").startswith("http://example.com")
    assert normalize_poll_url("POST http://example.com/hook", "g").startswith("http://example.com")


def test_pasting_inject_url_redirects_to_mock_poll_url():
    result = normalize_poll_url("http://localhost:5001/api/inject/Infosys", "Infosys")
    assert result == "http://127.0.0.1:5001/webhook/Infosys"


def test_strips_leading_junk_before_http():
    result = normalize_poll_url("here's your url: http://example.com/hook", "g")
    assert result == "http://example.com/hook"


def test_strips_trailing_text_after_url():
    result = normalize_poll_url("http://example.com/hook some trailing description", "g")
    assert result == "http://example.com/hook"


def test_group_placeholder_substitution():
    assert normalize_poll_url("http://example.com/{group}", "My Team") == "http://example.com/My%20Team"


def test_localhost_webhook_url_is_repointed_to_this_groups_mock_url():
    # A localhost /webhook/ URL pasted for a different group (or with a stale
    # slug) gets forced to this group's canonical mock URL.
    assert normalize_poll_url("http://localhost:5001/webhook/OtherGroup", "MyGroup") == "http://127.0.0.1:5001/webhook/MyGroup"
    assert normalize_poll_url("http://127.0.0.1:5001/webhook/whatever", "Team A") == "http://127.0.0.1:5001/webhook/Team%20A"


def test_non_localhost_webhook_url_is_left_alone():
    assert normalize_poll_url("http://example.com/webhook/OtherGroup", "MyGroup") == "http://example.com/webhook/OtherGroup"


def test_valid_url_passes_validation():
    ok, error = try_validate_poll_url("http://localhost:5001/webhook/Infosys")
    assert ok is True
    assert error == ""


def test_empty_url_fails_validation():
    ok, error = try_validate_poll_url("")
    assert ok is False
    assert "empty" in error.lower()


def test_non_http_scheme_fails_validation():
    ok, error = try_validate_poll_url("ftp://example.com/hook")
    assert ok is False


def test_malformed_url_fails_validation():
    ok, error = try_validate_poll_url("POST http://example.com/hook")
    assert ok is False


def test_inject_url_fails_validation_with_helpful_message():
    ok, error = try_validate_poll_url("http://localhost:5001/api/inject/Infosys")
    assert ok is False
    assert "inject" in error.lower()
