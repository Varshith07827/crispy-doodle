"""Port of WinSpark.Infrastructure.Services.WhatsApp.WhatsAppFetchApiClient.

Uses stdlib urllib (wrapped in asyncio.to_thread) rather than adding aiohttp/
httpx as a dependency — matches this port's existing preference for stdlib +
pywin32/uiautomation over pulling in new packages.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

from winspark.connectors import fetch_webhook_parser
from winspark.connectors.fetch_webhook_models import WebhookProbeResult, WhatsAppFetchApiResult
from winspark.connectors.fetch_webhook_url import try_validate_poll_url

_REQUEST_TIMEOUT_SECONDS = 15


async def fetch_async(fetch_url: str, api_key: str) -> WhatsAppFetchApiResult:
    if not fetch_url or not fetch_url.strip():
        return WhatsAppFetchApiResult.failed("Fetch URL is empty.")

    poll_url = fetch_url.strip()
    ok, url_error = try_validate_poll_url(poll_url)
    if not ok:
        return WhatsAppFetchApiResult.failed(url_error)

    try:
        status, body = await asyncio.to_thread(_get, poll_url, api_key)
    except Exception as ex:  # noqa: BLE001
        return WhatsAppFetchApiResult.failed(str(ex))

    if not (200 <= status < 300):
        return WhatsAppFetchApiResult.failed(f"HTTP {status}: {_truncate(body, 200)}")

    parsed = fetch_webhook_parser.parse(status, body)
    if parsed.is_error:
        return WhatsAppFetchApiResult.failed(parsed.error_message or "Parse error")

    if not parsed.has_message or not (parsed.message_text or "").strip():
        return WhatsAppFetchApiResult.blank(parsed.parse_strategy)

    return WhatsAppFetchApiResult.with_message(parsed.message_text.strip(), parsed.external_id, parsed.parse_strategy)


async def probe_async(fetch_url: str, api_key: str) -> WebhookProbeResult:
    if not fetch_url or not fetch_url.strip():
        return WebhookProbeResult.failed(0, "Webhook URL is empty.")

    poll_url = fetch_url.strip()
    ok, url_error = try_validate_poll_url(poll_url)
    if not ok:
        return WebhookProbeResult.failed(0, url_error)

    try:
        status, body = await asyncio.to_thread(_get, poll_url, api_key)
    except Exception as ex:  # noqa: BLE001
        return WebhookProbeResult.failed(0, str(ex))

    if not (200 <= status < 300):
        return WebhookProbeResult.failed(status, f"HTTP {status}: {_truncate(body, 120)}")

    return WebhookProbeResult.ok_result(status, f"HTTP {status} OK")


def _get(url: str, api_key: str) -> tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    if api_key and api_key.strip():
        request.add_header("Authorization", f"Bearer {api_key.strip()}")

    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", errors="replace") if ex.fp else ""
        return ex.code, body


def _truncate(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else value[:max_len] + "…"
