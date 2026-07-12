"""Tests WhatsAppFetchLocalMockServer against real HTTP requests (urllib) —
no mocking, an actual socket round-trip. Runs on any platform.
"""

import json
import urllib.error
import urllib.request

import pytest

from winspark.connectors.fetch_webhook_mock_server import WhatsAppFetchLocalMockServer


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read().decode("utf-8")


def _post(url: str, body: str) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read().decode("utf-8")


@pytest.fixture
def mock_server():
    server = WhatsAppFetchLocalMockServer()
    server.ensure_started(0)  # port 0 -> OS picks a free port; find it below
    # ThreadingHTTPServer picked a real port; read it back off the socket.
    port = server._httpd.server_address[1]
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def test_webhook_get_with_empty_queue_returns_empty_body(mock_server):
    _, base = mock_server
    status, body = _get(f"{base}/webhook/Infosys")
    assert status == 200
    assert body == ""


def test_inject_then_poll_dequeues_one_message(mock_server):
    server, base = mock_server

    status, body = _post(f"{base}/api/inject/Infosys", "hello from AI")
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert server.get_queued_count("Infosys") == 1

    status, body = _get(f"{base}/webhook/Infosys")
    assert status == 200
    assert json.loads(body)["message"] == "hello from AI"
    assert server.get_queued_count("Infosys") == 0


def test_webhook_get_is_destructive_read_fifo_order(mock_server):
    server, base = mock_server
    server.inject_message("Infosys", "first")
    server.inject_message("Infosys", "second")

    _, first = _get(f"{base}/webhook/Infosys")
    _, second = _get(f"{base}/webhook/Infosys")

    assert json.loads(first)["message"] == "first"
    assert json.loads(second)["message"] == "second"


def test_webhook_post_injects_raw_body_as_message(mock_server):
    server, base = mock_server
    status, _ = _post(f"{base}/webhook/Infosys", "raw injected text")
    assert status == 200

    _, dequeued = _get(f"{base}/webhook/Infosys")
    assert json.loads(dequeued)["message"] == "raw injected text"


def test_shared_inject_round_robins_across_configured_groups(mock_server):
    server, base = mock_server
    server.configure_round_robin_groups(["Infosys", "TCS"])

    status1, body1 = _post(f"{base}/api/inject", "msg one")
    status2, body2 = _post(f"{base}/api/inject", "msg two")

    assert status1 == 200 and status2 == 200
    assert json.loads(body1)["group"] == "Infosys"
    assert json.loads(body2)["group"] == "TCS"
    assert server.get_queued_count("Infosys") == 1
    assert server.get_queued_count("TCS") == 1


def test_shared_inject_with_no_groups_configured_fails(mock_server):
    _, base = mock_server
    status, body = _post(f"{base}/api/inject", "orphan message")
    assert status == 400
    assert "error" in json.loads(body)


def test_inject_with_empty_body_fails(mock_server):
    _, base = mock_server
    status, body = _post(f"{base}/api/inject/Infosys", "")
    assert status == 400


def test_api_status_reports_queue_state(mock_server):
    server, base = mock_server
    server.inject_message("Infosys", "one")
    server.inject_message("Infosys", "two")

    status, body = _get(f"{base}/api/status")
    payload = json.loads(body)
    assert status == 200
    assert payload["queued"] == 2
    assert payload["groups"]["Infosys"] == 2


def test_api_queue_reports_count_for_a_specific_group(mock_server):
    server, base = mock_server
    server.inject_message("Infosys", "one")

    status, body = _get(f"{base}/api/queue/Infosys")
    assert status == 200
    assert json.loads(body)["queued"] == 1


def test_unknown_path_returns_404(mock_server):
    _, base = mock_server
    status, body = _get(f"{base}/nonsense")
    assert status == 404


def test_get_on_post_only_endpoint_returns_405(mock_server):
    _, base = mock_server
    status, _ = _get(f"{base}/api/inject/Infosys")
    assert status == 405

def test_on_message_injected_callback_fires_for_a_post():
    server = WhatsAppFetchLocalMockServer()
    seen = []
    server.on_message_injected(seen.append)
    server.inject_message("Family", "hello")          # what a POST does
    assert seen == ["Family"]
    # empty / blank messages don't fire it
    server.inject_message("Family", "   ")
    assert seen == ["Family"]


def test_drain_pending_returns_plain_texts_and_empties_the_queue():
    server = WhatsAppFetchLocalMockServer()
    server.inject_message("Karthik", "first")
    server.inject_message("Karthik", "second")
    assert server.drain_pending("Karthik") == ["first", "second"]
    assert server.get_queued_count("Karthik") == 0           # drained
    assert server.drain_pending("Karthik") == []             # nothing left
