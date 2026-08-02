"""Verifies AutomationRuleMapper round-trips definitions through the same
JSON shape the .NET AutomationRuleMapper produces (camelCase typeId/parameters),
and that malformed JSON degrades to empty values instead of raising."""

from winspark.automation.mapper import to_definition, to_entity
from winspark.domain.automation import (
    AutomationActionDefinition,
    AutomationConditionDefinition,
    AutomationRuleDefinition,
    AutomationTriggerDefinition,
)
from winspark.domain.entities import AutomationRuleEntity


def test_round_trip_definition_to_entity_and_back():
    rule = AutomationRuleDefinition(
        id=0,
        name="Notify on Chrome",
        description="Shows a toast when Chrome opens",
        is_enabled=True,
        trigger=AutomationTriggerDefinition(type_id="trigger.window.opened", parameters={"processName": "chrome.exe"}),
        conditions=(AutomationConditionDefinition(type_id="condition.always", parameters={}),),
        actions=(AutomationActionDefinition(type_id="action.show_notification", parameters={"message": "Chrome opened"}),),
    )

    entity = to_entity(rule)
    assert entity.trigger_type_id == "trigger.window.opened"
    assert '"typeId"' in entity.conditions_json
    assert '"parameters"' in entity.actions_json

    roundtripped = to_definition(entity)
    assert roundtripped.name == rule.name
    assert roundtripped.trigger.type_id == rule.trigger.type_id
    assert roundtripped.trigger.parameters == {"processName": "chrome.exe"}
    assert len(roundtripped.conditions) == 1
    assert roundtripped.conditions[0].type_id == "condition.always"
    assert len(roundtripped.actions) == 1
    assert roundtripped.actions[0].parameters == {"message": "Chrome opened"}


def test_to_entity_carries_id_for_updates_but_uses_none_sentinel_for_new_rules():
    # Matches C#'s AutomationRuleMapper.ToEntity, adapted to this port's
    # None-means-"not yet inserted" convention (id=0 in C# is the same "new
    # rule" sentinel, since AUTOINCREMENT ids start at 1).
    assert to_entity(AutomationRuleDefinition(id=42, name="Existing rule")).id == 42
    assert to_entity(AutomationRuleDefinition(id=0, name="New rule")).id is None


def test_malformed_json_degrades_to_empty_instead_of_raising():
    entity = AutomationRuleEntity(
        name="Corrupt",
        trigger_type_id="trigger.window.opened",
        trigger_config_json="{not valid json",
        conditions_json="also not json",
        actions_json="[1, 2,",
    )

    definition = to_definition(entity)
    assert definition.trigger.parameters == {}
    assert definition.conditions == ()
    assert definition.actions == ()


def test_empty_json_defaults_are_empty_collections():
    entity = AutomationRuleEntity(name="Blank", trigger_type_id="trigger.window.opened", trigger_config_json="", conditions_json="", actions_json="")
    definition = to_definition(entity)
    assert definition.trigger.parameters == {}
    assert definition.conditions == ()
    assert definition.actions == ()
