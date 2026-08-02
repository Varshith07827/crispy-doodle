"""Verifies TriggerIndexedRuleIndex: enabled-only indexing, case-insensitive
trigger lookup, and invalidate() clearing state."""

from winspark.automation.rule_index import TriggerIndexedRuleIndex
from winspark.domain.automation import AutomationRuleDefinition, AutomationTriggerDefinition


def _rule(id_: int, trigger_type_id: str, enabled: bool = True) -> AutomationRuleDefinition:
    return AutomationRuleDefinition(id=id_, name=f"rule-{id_}", is_enabled=enabled, trigger=AutomationTriggerDefinition(type_id=trigger_type_id))


def test_rebuild_indexes_only_enabled_rules_by_trigger():
    index = TriggerIndexedRuleIndex()
    rules = [
        _rule(1, "trigger.window.opened"),
        _rule(2, "trigger.window.opened"),
        _rule(3, "trigger.window.closed"),
        _rule(4, "trigger.window.opened", enabled=False),
    ]
    index.rebuild(rules)

    opened = index.get_rules_for_trigger("trigger.window.opened")
    assert {r.id for r in opened} == {1, 2}

    closed = index.get_rules_for_trigger("trigger.window.closed")
    assert {r.id for r in closed} == {3}

    assert {r.id for r in index.get_all_enabled()} == {1, 2, 3}


def test_trigger_lookup_is_case_insensitive():
    index = TriggerIndexedRuleIndex()
    index.rebuild([_rule(1, "Trigger.Window.Opened")])
    assert len(index.get_rules_for_trigger("trigger.window.opened")) == 1
    assert len(index.get_rules_for_trigger("TRIGGER.WINDOW.OPENED")) == 1


def test_unknown_trigger_returns_empty_list():
    index = TriggerIndexedRuleIndex()
    index.rebuild([_rule(1, "trigger.window.opened")])
    assert index.get_rules_for_trigger("trigger.nonexistent") == []


def test_invalidate_clears_index():
    index = TriggerIndexedRuleIndex()
    index.rebuild([_rule(1, "trigger.window.opened")])
    index.invalidate()
    assert index.get_all_enabled() == []
    assert index.get_rules_for_trigger("trigger.window.opened") == []
