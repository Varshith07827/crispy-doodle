"""Verifies ShowNotificationAction's message-building and unread-gate skip
logic (ported from BuiltInActions.cs's ShowNotificationAction) without
touching the real Win32 balloon-tip call, which is monkeypatched out."""

import pytest

import winspark.automation.actions as actions_module
from winspark.automation.actions import ShowNotificationAction
from winspark.constants import AutomationTypeIds, BusEventTypes
from winspark.domain.automation import AutomationActionDefinition, AutomationContext, AutomationRuleDefinition
from winspark.domain.entities import EventEntity
from winspark.domain.enums import EventTypeKind


@pytest.fixture(autouse=True)
def _stub_balloon(monkeypatch):
    calls = []
    monkeypatch.setattr(actions_module, "_show_balloon_notification", lambda title, message: calls.append((title, message)))
    return calls


def _context(window_title: str, process_name: str, rule=None) -> AutomationContext:
    return AutomationContext(
        event_type=BusEventTypes.WINDOW_OPENED,
        rule=rule,
        window_event=EventEntity(event_type=EventTypeKind.WINDOW_OPENED, process_name=process_name, window_title=window_title),
    )


@pytest.mark.asyncio
async def test_builds_default_message_from_event_type_and_app_name(_stub_balloon):
    action = ShowNotificationAction()
    context = _context(window_title="New Tab", process_name="chrome.exe")
    definition = AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_SHOW_NOTIFICATION)

    result = await action.execute_async(context, definition)

    assert result.success is True
    assert _stub_balloon == [("winSpark", "Chrome window opened — New Tab")]


@pytest.mark.asyncio
async def test_custom_message_is_used_when_not_generic(_stub_balloon):
    action = ShowNotificationAction()
    context = _context(window_title="Inbox", process_name="outlook.exe")
    definition = AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_SHOW_NOTIFICATION, parameters={"message": "You have 3 new invoices to review"})

    result = await action.execute_async(context, definition)

    assert result.success is True
    assert _stub_balloon[0][1] == "You have 3 new invoices to review"


@pytest.mark.asyncio
async def test_skips_when_rule_implies_unread_but_title_has_no_unread_count(_stub_balloon):
    action = ShowNotificationAction()
    rule = AutomationRuleDefinition(name="Notify on unread WhatsApp")
    context = _context(window_title="WhatsApp", process_name="whatsapp.exe", rule=rule)
    definition = AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_SHOW_NOTIFICATION)

    result = await action.execute_async(context, definition)

    assert result.success is True
    assert "Skipped" in result.message
    assert _stub_balloon == []


@pytest.mark.asyncio
async def test_fires_when_rule_implies_unread_and_title_has_unread_count(_stub_balloon):
    action = ShowNotificationAction()
    rule = AutomationRuleDefinition(name="Notify on unread WhatsApp")
    context = _context(window_title="WhatsApp (4 unread)", process_name="whatsapp.exe", rule=rule)
    definition = AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_SHOW_NOTIFICATION)

    result = await action.execute_async(context, definition)

    assert result.success is True
    assert len(_stub_balloon) == 1


@pytest.mark.asyncio
async def test_generic_message_containing_unread_with_no_count_in_title_is_skipped(_stub_balloon):
    action = ShowNotificationAction()
    context = _context(window_title="Slack", process_name="slack.exe")
    definition = AutomationActionDefinition(
        type_id=AutomationTypeIds.ACTION_SHOW_NOTIFICATION, parameters={"message": "you have unread messages"}
    )

    result = await action.execute_async(context, definition)

    assert result.success is True
    assert "Skipped" in result.message
    assert _stub_balloon == []
