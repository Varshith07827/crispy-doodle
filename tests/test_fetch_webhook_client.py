"""Tests fetch_webhook_client against a real local HTTP server (stdlib
http.server) — no mocking, an actual socket round-trip. Runs on any platform.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from winspark.connectors import fetch_webhook_client


class _Handler(BaseHTTPRequestHandler):
    server_version = "TestServer/1.0"

    def log_message(self, format, *args):  # noqa: A002 - silence test server logs
        pass

    def do_GET(self):  # noqa: N802 - stdlib method name
        if self.path == "/plain":
            self._respond(200, "hello from the webhook")
        elif self.path == "/json":
            self._respond(200, json.dumps({"message": "hi from json"}), content_type="application/json")
        elif self.path == "/empty":
            self._respond(204, "")
        elif self.path == "/error":
            self._respond(500, "boom")
        elif self.path == "/auth":
            auth = self.headers.get("Authorization", "")
            if auth == "Bearer secret123":
                self._respond(200, "authorized")
            else:
                self._respond(401, "unauthorized")
        else:
            self._respond(404, "not found")

    def _respond(self, status: int, body: str, content_type: str = "text/plain"):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_fetch_plain_text(server):
    result = await fetch_webhook_client.fetch_async(f"{server}/plain", "")
    assert result.has_message is True
    assert result.message == "hello from the webhook"


@pytest.mark.asyncio
async def test_fetch_json_message(server):
    result = await fetch_webhook_client.fetch_async(f"{server}/json", "")
    assert result.has_message is True
    assert result.message == "hi from json"


@pytest.mark.asyncio
async def test_fetch_204_is_blank_not_error(server):
    result = await fetch_webhook_client.fetch_async(f"{server}/empty", "")
    assert result.is_error is False
    assert result.has_message is False


@pytest.mark.asyncio
async def test_fetch_http_error_status_is_failed(server):
    result = await fetch_webhook_client.fetch_async(f"{server}/error", "")
    assert result.is_error is True
    assert "500" in result.error_message


@pytest.mark.asyncio
async def test_fetch_sends_bearer_auth_header(server):
    unauthorized = await fetch_webhook_client.fetch_async(f"{server}/auth", "")
    assert unauthorized.is_error is True

    authorized = await fetch_webhook_client.fetch_async(f"{server}/auth", "secret123")
    assert authorized.is_error is False
    assert authorized.message == "authorized"


@pytest.mark.asyncio
async def test_fetch_empty_url_fails_without_network_call():
    result = await fetch_webhook_client.fetch_async("", "")
    assert result.is_error is True
    assert "empty" in result.error_message.lower()


@pytest.mark.asyncio
async def test_probe_ok_and_failed(server):
    ok = await fetch_webhook_client.probe_async(f"{server}/plain", "")
    assert ok.ok is True
    assert ok.status_code == 200

    failed = await fetch_webhook_client.probe_async(f"{server}/error", "")
    assert failed.ok is False
    assert failed.status_code == 500
