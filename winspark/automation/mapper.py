"""Port of WinSpark.Infrastructure.Automation.AutomationRuleMapper.

Entity <-> definition mapping, with the same "swallow malformed JSON and fall
back to an empty value" behavior as the .NET version (a corrupt row shouldn't
crash rule loading).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from winspark.domain.automation import (
    AutomationActionDefinition,
    AutomationConditionDefinition,
    AutomationRuleDefinition,
    AutomationTriggerDefinition,
)
from winspark.domain.entities import AutomationRuleEntity


def to_definition(entity: AutomationRuleEntity) -> AutomationRuleDefinition:
    return AutomationRuleDefinition(
        id=entity.id or 0,
        name=entity.name,
        description=entity.description,
        is_enabled=entity.is_enabled,
        trigger=AutomationTriggerDefinition(
            type_id=entity.trigger_type_id,
            parameters=_deserialize_dict(entity.trigger_config_json),
        ),
        conditions=tuple(
            AutomationConditionDefinition(type_id=d.get("typeId", ""), parameters=d.get("parameters", {}))
            for d in _deserialize_list(entity.conditions_json)
        ),
        actions=tuple(
            AutomationActionDefinition(type_id=d.get("typeId", ""), parameters=d.get("parameters", {}))
            for d in _deserialize_list(entity.actions_json)
        ),
    )


def to_entity(definition: AutomationRuleDefinition) -> AutomationRuleEntity:
    now = datetime.now(timezone.utc)
    return AutomationRuleEntity(
        id=definition.id or None,
        name=definition.name,
        description=definition.description,
        is_enabled=definition.is_enabled,
        trigger_type_id=definition.trigger.type_id,
        trigger_config_json=json.dumps(definition.trigger.parameters or {}),
        conditions_json=json.dumps(
            [{"typeId": c.type_id, "parameters": c.parameters} for c in definition.conditions]
        ),
        actions_json=json.dumps(
            [{"typeId": a.type_id, "parameters": a.parameters} for a in definition.actions]
        ),
        created_at_utc=now,
        updated_at_utc=now,
    )


def _deserialize_dict(text: str) -> dict[str, str]:
    if not text or not text.strip():
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _deserialize_list(text: str) -> list[dict]:
    if not text or not text.strip():
        return []
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
