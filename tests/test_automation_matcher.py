"""Verifies AutomationRuleMatcher (trigger-parameter filtering, unread gate)
and the extract_unread_count regex ladder ported from CommunicationWindowParser."""

from winspark.automation.matcher import extract_unread_count, matches_trigger_parameters, passes_unread_gate
from winspark.domain.automation import (
    AutomationActionDefinition,
    AutomationContext,
    AutomationRuleDefinition,
    AutomationTriggerDefinition,
)
from winspark.domain.entities import EventEntity
from winspark.domain.enums import EventTypeKind


def _context(window_title: str = "", process_name: str = "") -> AutomationContext:
    return AutomationContext(
        window_event=EventEntity(
            event_type=EventTypeKind.WINDOW_OPENED, process_name=process_name, window_title=window_title
        )
    )


def test_matches_trigger_parameters_filters_by_process_name_case_insensitively():
    trigger = AutomationTriggerDefinition(parameters={"processName": "CHROME"})
    assert matches_trigger_parameters(trigger, _context(process_name="chrome.exe"))
    assert not matches_trigger_parameters(trigger, _context(process_name="notepad.exe"))


def test_matches_trigger_parameters_filters_by_title_substring():
    trigger = AutomationTriggerDefinition(parameters={"title": "inbox"})
    assert matches_trigger_parameters(trigger, _context(window_title="Inbox — email@example.com"))
    assert not matches_trigger_parameters(trigger, _context(window_title="Sent Items"))


def test_matches_trigger_parameters_no_filters_always_passes():
    assert matches_trigger_parameters(AutomationTriggerDefinition(), _context(window_title="anything"))


def test_extract_unread_count_recognizes_common_patterns():
    assert extract_unread_count("WhatsApp (3 unread)") == 3
    assert extract_unread_count("Inbox (12 new messages)") == 12
    assert extract_unread_count("Slack (5 messages)") == 5
    assert extract_unread_count("2 unread — Teams") == 2
    assert extract_unread_count("John Doe (4) WhatsApp") == 4
    assert extract_unread_count("General - WhatsApp") == 0
    assert extract_unread_count("Notepad") == 0
    assert extract_unread_count("") == 0
    assert extract_unread_count("(0 unread)") == 0


def test_passes_unread_gate_only_applies_to_rules_implying_unread():
    non_unread_rule = AutomationRuleDefinition(name="Notify on Chrome open")
    assert passes_unread_gate(non_unread_rule, _context(window_title="Chrome"))

    unread_by_name_rule = AutomationRuleDefinition(name="Notify on unread WhatsApp")
    assert not passes_unread_gate(unread_by_name_rule, _context(window_title="WhatsApp"))
    assert passes_unread_gate(unread_by_name_rule, _context(window_title="WhatsApp (2 unread)"))

    unread_by_action_message_rule = AutomationRuleDefinition(
        name="Chrome rule",
        actions=(AutomationActionDefinition(type_id="action.show_notification", parameters={"message": "you have unread mail"}),),
    )
    assert not passes_unread_gate(unread_by_action_message_rule, _context(window_title="Gmail"))
    assert passes_unread_gate(unread_by_action_message_rule, _context(window_title="Gmail (3 unread)"))
