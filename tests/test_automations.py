"""Unit tests for the saved-automation persistence contract: how an Automation
maps to/from the generic AutomationRules row (a "manual" trigger + the action
details in ActionsJson), and the plain-English summary. Pure functions — no DB,
no engine, no Windows."""

import json

from winspark.ui.engine_host import (
    AUTOMATION_APP_ACTION,
    AUTOMATION_WHATSAPP,
    Automation,
    _automation_to_rule,
    _rule_to_automation,
)
from winspark.domain.entities import AutomationRuleEntity


def test_whatsapp_automation_round_trips():
    original = Automation(
        id=3, name="Morning ping", kind=AUTOMATION_WHATSAPP,
        target="Family", target_display="Family", instruction="Good morning!",
        enabled=True,
    )
    rule = _automation_to_rule(original)
    assert rule.trigger_type_id == "manual"
    assert json.loads(rule.actions_json)[0]["kind"] == AUTOMATION_WHATSAPP

    back = _rule_to_automation(rule)
    assert back == original


def test_app_action_automation_round_trips():
    original = Automation(
        id=9, name="Search flights", kind=AUTOMATION_APP_ACTION,
        target="chrome.exe", target_display="Chrome",
        instruction="search Google for cheap flights", enabled=False,
    )
    assert _rule_to_automation(_automation_to_rule(original)) == original


def test_rule_description_is_the_human_summary():
    rule = _automation_to_rule(Automation(
        None, "x", AUTOMATION_APP_ACTION, "chrome.exe", "Chrome", "do a thing"))
    assert rule.description == "In Chrome: do a thing"


def test_summary_wording():
    wa = Automation(1, "n", AUTOMATION_WHATSAPP, "Mum", "Mum", "hi there")
    assert wa.summary() == "Message “Mum”: hi there"
    app = Automation(1, "n", AUTOMATION_APP_ACTION, "chrome.exe", "Chrome", "search")
    assert app.summary() == "In Chrome: search"


def test_malformed_actions_json_degrades_to_empty_app_action():
    bad = _rule_to_automation(AutomationRuleEntity(name="broken", actions_json="{ not json"))
    assert bad.kind == AUTOMATION_APP_ACTION
    assert bad.instruction == ""
    assert bad.name == "broken"  # still listable, not dropped


def test_crud_against_the_real_automation_rules_table(tmp_path):
    """The encode/decode contract against the actual SQLite schema + repository
    — insert, list, update, delete all round-trip through real SQL."""
    from winspark.data.connection import ConnectionFactory
    from winspark.data.repositories import AutomationRuleRepository

    factory = ConnectionFactory(tmp_path / "auto.db")
    factory.initialize_schema()
    repo = AutomationRuleRepository(factory)

    new_id = repo.insert(_automation_to_rule(Automation(
        None, "Ping Mum", AUTOMATION_WHATSAPP, "Mum", "Mum", "hello", True)))
    listed = [_rule_to_automation(r) for r in repo.get_all()]
    assert len(listed) == 1 and listed[0].name == "Ping Mum" and listed[0].instruction == "hello"

    repo.update(_automation_to_rule(Automation(
        new_id, "Ping Mum", AUTOMATION_WHATSAPP, "Mum", "Mum", "hi again", True)))
    assert _rule_to_automation(repo.get_by_id(new_id)).instruction == "hi again"

    repo.delete(new_id)
    assert repo.get_all() == []
