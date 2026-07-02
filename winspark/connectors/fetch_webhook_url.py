"""Port of WinSpark.Domain.Utilities.FetchWebhookUrlNormalizer.

Fixes common mistakes when pasting Fetch-Webhook poll URLs (leading "GET "/
"POST ", pasting the POST-only inject URL instead of the GET poll URL,
trailing whitespace/description text, {group} placeholders).
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

from winspark.connectors.fetch_webhook_models import FetchWebhookDefaults


def normalize_poll_url(raw: str | None, group_name: str) -> str:
    group_name = group_name if group_name and group_name.strip() else "group"

    if not raw or not raw.strip():
        return FetchWebhookDefaults.mock_url_for_group(group_name)

    slug = quote(group_name.strip())
    s = raw.strip()

    if s.lower().startswith("post "):
        s = s[5:].lstrip()
    if s.lower().startswith("get "):
        s = s[4:].lstrip()

    if "/api/inject/" in s.lower() or "/api/inject-batch/" in s.lower():
        return FetchWebhookDefaults.mock_url_for_group(group_name)

    lowered = s.lower()
    http_idx = lowered.find("http://")
    https_idx = lowered.find("https://")
    start = http_idx if http_idx >= 0 else https_idx
    if start > 0:
        s = s[start:]

    space = s.find(" ")
    if space > 0:
        s = s[:space]

    s = (
        s.replace("{group}", slug)
        .replace("{Group}", slug)
        .replace("{GROUP}", slug)
        .replace("{groupname}", slug)
        .replace("{groupName}", slug)
        .replace("{GroupName}", slug)
        .replace("group-name", slug)
    )

    return s.strip()


def try_validate_poll_url(url: str) -> tuple[bool, str]:
    if not url or not url.strip():
        return False, "Poll URL is empty."

    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None

    if not parsed or not parsed.scheme or not parsed.netloc:
        return False, (
            "Poll URL must start with http:// or https:// (not POST …). "
            "Example: http://localhost:5001/webhook/Infosys"
        )

    if parsed.scheme not in ("http", "https"):
        return False, "Poll URL must use http or https."

    if "/api/inject" in url.lower():
        return False, "That is the inject URL (POST only). Poll URL example: http://localhost:5001/webhook/YourGroup"

    return True, ""
