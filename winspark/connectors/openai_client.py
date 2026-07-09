"""Chat-completions client for OpenAI-compatible AI services (OpenAI, Groq).

winSpark calls the service itself using an app-wide API key, either to reply to
an incoming message or to generate one from a prompt. `base_url` selects the
provider (both expose the same /chat/completions and /models endpoints and
Bearer auth), so one client covers all of them.

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

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_REQUEST_TIMEOUT_SECONDS = 30
# Some providers (confirmed live: Groq) sit behind Cloudflare, which blocks
# Python's default "Python-urllib/x.y" User-Agent as bot traffic — the request
# never reaches the provider at all; Cloudflare itself returns 403 "error code:
# 1010" (not a JSON error body, so it doesn't look like an auth problem, but
# the API key is never even checked). A normal browser User-Agent passes
# straight through. Confirmed live: the exact same request with a valid Groq
# key got 403 with the default UA and 200 with this one.
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _chat_completions_url(base_url: str) -> str:
    return f"{(base_url or _DEFAULT_BASE_URL).rstrip('/')}/chat/completions"


def _models_url(base_url: str) -> str:
    return f"{(base_url or _DEFAULT_BASE_URL).rstrip('/')}/models"

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
    base_url: str = _DEFAULT_BASE_URL,
    temperature: float = 0.7,
) -> OpenAiResult:
    """Ask the AI service for one message. `system_prompt` is the per-chat
    instructions; `user_message` is the incoming message to reply to (reply
    mode) or a short generate instruction (generate mode). Returns the reply
    text or a plain error."""
    if not (api_key or "").strip():
        return OpenAiResult.failed("No AI key set — add it in the AI settings.")

    messages = []
    system = (system_prompt or "").strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": (user_message or "").strip() or _DEFAULT_GENERATE_INSTRUCTION})

    payload = {"model": (model or "").strip(), "messages": messages, "temperature": max(0.0, min(1.0, temperature))}

    try:
        status, body = await asyncio.to_thread(_post_json, _chat_completions_url(base_url), api_key, payload)
    except Exception as ex:  # noqa: BLE001
        return OpenAiResult.failed(_friendly_network_error(ex))

    if not (200 <= status < 300):
        return OpenAiResult.failed(_friendly_http_error(status, body))

    text = _extract_reply_text(body)
    if not text:
        return OpenAiResult.failed("The AI service returned an empty reply.")
    return OpenAiResult.succeeded(text)


async def classify_intent_match_async(
    api_key: str,
    model: str,
    intent: str,
    message: str,
    base_url: str = _DEFAULT_BASE_URL,
) -> Optional[bool]:
    """Decide whether `message` matches `intent` (the "wait for" phrase), by
    meaning rather than exact words. Returns True/False, or None if the call
    fails (so the caller can fall back to literal matching)."""
    if not (api_key or "").strip() or not (intent or "").strip() or not (message or "").strip():
        return None

    system = (
        "You decide whether a chat message matches what the user is waiting for. "
        "The match is by meaning, not exact wording. Answer with only 'yes' or 'no'."
    )
    user = f"Waiting for: {intent.strip()}\nMessage received: {message.strip()}\nDoes the message match? Answer yes or no."
    payload = {
        "model": (model or "").strip(),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 3,
    }

    try:
        status, body = await asyncio.to_thread(_post_json, _chat_completions_url(base_url), api_key, payload)
    except Exception:  # noqa: BLE001
        return None
    if not (200 <= status < 300):
        return None

    answer = _extract_reply_text(body).strip().lower()
    if answer.startswith("yes"):
        return True
    if answer.startswith("no"):
        return False
    return None


async def complete_json_async(
    api_key: str,
    model: str,
    system: str,
    user: str,
    base_url: str = _DEFAULT_BASE_URL,
    temperature: float = 0.0,
) -> OpenAiResult:
    """One completion where the reply is expected to be JSON (used by the
    acting agent). Temperature defaults to 0 for determinism; callers may pass
    the user's configured response style. The caller parses and validates —
    this just returns the raw text or a plain error."""
    if not (api_key or "").strip():
        return OpenAiResult.failed("No AI key set — add it in the AI settings.")

    payload = {
        "model": (model or "").strip(),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": max(0.0, min(1.0, temperature)),
    }
    try:
        status, body = await asyncio.to_thread(_post_json, _chat_completions_url(base_url), api_key, payload)
    except Exception as ex:  # noqa: BLE001
        return OpenAiResult.failed(_friendly_network_error(ex))
    if not (200 <= status < 300):
        return OpenAiResult.failed(_friendly_http_error(status, body))

    text = _extract_reply_text(body)
    if not text:
        return OpenAiResult.failed("The AI service returned an empty reply.")
    return OpenAiResult.succeeded(text)


async def probe_async(api_key: str, model: str, base_url: str = _DEFAULT_BASE_URL) -> OpenAiResult:
    """Cheap key check for the "Test connection" button: list models (no tokens
    billed). Confirms the key is valid and, if a model is given, that it exists."""
    if not (api_key or "").strip():
        return OpenAiResult.failed("No AI key set — paste your key first.")

    try:
        status, body = await asyncio.to_thread(_get, _models_url(base_url), api_key)
    except Exception as ex:  # noqa: BLE001
        return OpenAiResult.failed(_friendly_network_error(ex))

    if not (200 <= status < 300):
        return OpenAiResult.failed(_friendly_http_error(status, body))

    wanted = (model or "").strip()
    if wanted and not _models_include(body, wanted):
        return OpenAiResult.failed(f"The key works, but the model \"{wanted}\" isn't available to it.")
    return OpenAiResult.succeeded("Connected")


def _post_json(url: str, api_key: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Authorization", f"Bearer {api_key.strip()}")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", _USER_AGENT)
    return _send(request)


def _get(url: str, api_key: str) -> tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {api_key.strip()}")
    request.add_header("User-Agent", _USER_AGENT)
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
        return "The key was rejected — check that it's correct."
    if status == 403:
        # A real API-level 403 (key valid but lacks permission) comes back as
        # JSON with a message, same as any other error — detail covers that.
        # An EMPTY/non-JSON body on 403 means the request was blocked before
        # it ever reached the AI service (confirmed live: Cloudflare in front
        # of Groq blocks non-browser HTTP clients — the key is never checked).
        return detail or "The connection was blocked before it reached the AI service — this isn't a problem with your key."
    if status == 429:
        return "The service is rate-limiting or your quota is used up — try again later."
    if status == 404:
        return detail or "That model wasn't found."
    if 500 <= status < 600:
        return "The AI service had a server problem — try again in a moment."
    return detail or f"The AI service returned an error (code {status})."


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
    return f"Couldn't reach the AI service — {ex}".strip()
