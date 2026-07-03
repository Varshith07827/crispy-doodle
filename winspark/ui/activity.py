"""Turns the engine's neutral activity events into plain-English lines for the
activity log — the UI/logging boundary. Pure functions, no Qt, so they're
testable and reusable. The engine emits stable `kind` tokens; wording lives
here, so a future adapter gets the same friendly phrasing for free.
"""

from __future__ import annotations


def friendly_reason(detail: str) -> str:
    """Rewrite a technical failure detail into something a person understands."""
    if not detail:
        return ""
    d = detail.lower()
    if "http 5" in d or "returned an error" in d:
        return "the website returned an error"
    if "http 4" in d:
        return "the website rejected the request"
    if "not running" in d:
        return "the app isn't open"
    if "not found" in d and "chat" in d:
        return "the chat couldn't be found"
    if "foreground" in d or "not in front" in d:
        return "the app wasn't in front"
    if "typed but not sent" in d:
        return "the message was typed but didn't send"
    if "empty" in d or "no message" in d:
        return "there was nothing to send"
    if "timed out" in d or "timeout" in d:
        return "it took too long to respond"
    if "url" in d:
        return "the message source address looks wrong"
    # Fall back to the raw detail, trimmed.
    return detail if len(detail) <= 80 else detail[:80] + "…"


def describe_activity(chat: str, kind: str, detail: str = "") -> str:
    """Plain-English line for one activity event. `chat` may be empty for
    app-wide events (automation on/off)."""
    who = chat.strip()
    reason = friendly_reason(detail)
    suffix = f" — {reason}" if reason else ""

    if kind == "automation_on":
        return "Automation started — watching for new messages"
    if kind == "automation_off":
        return "Automation stopped"
    if kind == "checking":
        return "Checking for a new message…"
    if kind == "received":
        return f"New message received for {who}" if who else "New message received"
    if kind == "sending":
        return f"Sending the message to {who}…" if who else "Sending the message…"
    if kind == "sent":
        return f"Sent the message to {who}" if who else "Sent the message"
    if kind == "retrying":
        return f"Trying again to reach {who}…" if who else "Trying again…"
    if kind == "send_failed":
        base = f"Couldn't send the message to {who}" if who else "Couldn't send the message"
        return base + suffix
    if kind == "source_error":
        return "Couldn't reach the message source" + suffix
    if kind == "watch_matched":
        found = f" — “{detail}”" if detail else ""
        return (f"Found what you were watching for in {who}" if who else "Found what you were watching for") + found
    if kind == "watch_error":
        return (f"Problem while watching {who}" if who else "Problem while watching") + suffix
    if kind == "agent_run":
        return f"Did something for you: {detail}" if detail else "Did something for you"
    # Unknown kind — show something rather than nothing.
    return (f"{who}: " if who else "") + (kind.replace("_", " ").capitalize()) + suffix
