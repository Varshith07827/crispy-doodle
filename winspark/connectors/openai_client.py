"""OpenAI chat-completions client — the second reply source (alongside the
Fetch-Webhook one). winSpark calls OpenAI itself using an app-wide API key,
either to reply to an incoming message or to generate one from a prompt.

Uses stdlib urllib (wrapped in asyncio.to_thread), matching this port's
existing preference for stdlib over adding aiohttp/httpx/the openai SDK — the
Chat Completions endpoint is a single POST, so a dependency isn't warranted.

Errors are returned as plain, user-facing strings (no HTTP jargon) so the GUI
and activity log can show them directly.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
_MODELS_URL = "https://api.openai.com/v1/models"
_REQUEST_TIMEOUT_SECONDS = 30

_DEFAULT_GENERATE_INSTRUCTION = "Write a short, friendly WhatsApp message."


@dataclass(frozen=True, slots=True)
class OpenAiResult:
    ok: bool = False
    text: str = ""
    error: str = ""

    @staticmethod
    def succeeded(text: str) -> "OpenAiResult":
        return OpenAiResult(ok=True, text=text)

    @staticmethod
    def failed(error: str) -> "OpenAiResult":
        return OpenAiResult(ok=False, error=error)


async def generate_reply_async(
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
) -> OpenAiResult:
    """Ask OpenAI for one message. `system_prompt` is the per-chat instructions;
    `user_message` is the incoming message to reply to (reply mode) or a short
    generate instruction (generate mode). Returns the reply text or a plain
    error."""
    if not (api_key or "").strip():
        return OpenAiResult.failed("No OpenAI key set — add it in the OpenAI settings.")

    messages = []
    system = (system_prompt or "").strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": (user_message or "").strip() or _DEFAULT_GENERATE_INSTRUCTION})

    payload = {"model": (model or "").strip(), "messages": messages, "temperature": 0.7}

    try:
        status, body = await asyncio.to_thread(_post_json, _CHAT_COMPLETIONS_URL, api_key, payload)
    except Exception as ex:  # noqa: BLE001
        return OpenAiResult.failed(_friendly_network_error(ex))

    if not (200 <= status < 300):
        return OpenAiResult.failed(_friendly_http_error(status, body))

    text = _extract_reply_text(body)
    if not text:
        return OpenAiResult.failed("OpenAI returned an empty reply.")
    return OpenAiResult.succeeded(text)


async def probe_async(api_key: str, model: str) -> OpenAiResult:
    """Cheap key check for the "Test connection" button: list models (no tokens
    billed). Confirms the key is valid and, if a model is given, that it exists."""
    if not (api_key or "").strip():
        return OpenAiResult.failed("No OpenAI key set — paste your key first.")

    try:
        status, body = await asyncio.to_thread(_get, _MODELS_URL, api_key)
    except Exception as ex:  # noqa: BLE001
        return OpenAiResult.failed(_friendly_network_error(ex))

    if not (200 <= status < 300):
        return OpenAiResult.failed(_friendly_http_error(status, body))

    wanted = (model or "").strip()
    if wanted and not _models_include(body, wanted):
        return OpenAiResult.failed(f"The key works, but the model \"{wanted}\" isn't available to it.")
    return OpenAiResult.succeeded("Connected to OpenAI")


def _post_json(url: str, api_key: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Authorization", f"Bearer {api_key.strip()}")
    request.add_header("Content-Type", "application/json")
    return _send(request)


def _get(url: str, api_key: str) -> tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {api_key.strip()}")
    return _send(request)


def _send(request: urllib.request.Request) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", errors="replace") if ex.fp else ""
        return ex.code, body


def _extract_reply_text(body: str) -> str:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def _models_include(body: str, model: str) -> bool:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return True  # can't tell — don't block on it
    ids = {(m.get("id") or "") for m in (data.get("data") or [])}
    return model in ids if ids else True


def _friendly_http_error(status: int, body: str) -> str:
    detail = _extract_error_message(body)
    if status == 401:
        return "OpenAI rejected the key — check that it's correct."
    if status == 429:
        return "OpenAI is rate-limiting or your quota is used up — try again later."
    if status == 404:
        return detail or "That OpenAI model wasn't found."
    if 500 <= status < 600:
        return "OpenAI had a server problem — try again in a moment."
    return detail or f"OpenAI returned an error (code {status})."


def _extract_error_message(body: str) -> Optional[str]:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        return (error.get("message") or "").strip() or None
    return None


def _friendly_network_error(ex: Exception) -> str:
    return f"Couldn't reach OpenAI — {ex}".strip()
