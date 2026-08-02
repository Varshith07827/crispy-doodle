"""Port of WinSpark.Infrastructure.Automation.AutomationRuleMatcher, plus the
one CommunicationWindowParser method it depends on (ExtractUnreadCount) —
pulled in standalone rather than the full WinSpark.AI communication-agent
layer, which isn't ported.
"""

from __future__ import annotations

import re

from winspark.domain.automation import AutomationContext, AutomationRuleDefinition, AutomationTriggerDefinition

_UNREAD_PATTERNS = (
    re.compile(r"\((\d+)\s+unread\)", re.IGNORECASE),
    re.compile(r"\((\d+)\s+new(?:\s+messages?)?\)", re.IGNORECASE),
    re.compile(r"\((\d+)\s+messages?\)", re.IGNORECASE),
    re.compile(r"(\d+)\s+unread\b", re.IGNORECASE),
    re.compile(r"\((\d+)\)\s*(?:WhatsApp\b|unread\b|$)", re.IGNORECASE),
    re.compile(r"\((\d+)\)(?:\s*[-–—|]\s*)?(?:WhatsApp\b|$)", re.IGNORECASE),
    re.compile(r"\((\d+)\)\s*$", re.IGNORECASE),
)


def extract_unread_count(title: str) -> int:
    """Port of CommunicationWindowParser.ExtractUnreadCount."""
    if not title or not title.strip():
        return 0

    for pattern in _UNREAD_PATTERNS:
        match = pattern.search(title)
        if match:
            count = int(match.group(1))
            if count > 0:
                return count

    return 0


def matches_trigger_parameters(trigger: AutomationTriggerDefinition, context: AutomationContext) -> bool:
    process_name = trigger.parameters.get("processName")
    if process_name and process_name.strip():
        if process_name.lower() not in context.process_name.lower():
            return False

    title = trigger.parameters.get("title")
    if title and title.strip():
        if title.lower() not in context.window_title.lower():
            return False

    return True


def passes_unread_gate(rule: AutomationRuleDefinition, context: AutomationContext) -> bool:
    if not _rule_implies_unread(rule):
        return True

    return extract_unread_count(context.window_title) > 0


def _rule_implies_unread(rule: AutomationRuleDefinition) -> bool:
    if "unread" in rule.name.lower():
        return True

    for action in rule.actions:
        message = action.parameters.get("message")
        if message and "unread" in message.lower():
            return True

    return False
