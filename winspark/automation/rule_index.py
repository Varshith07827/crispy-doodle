"""Port of WinSpark.Infrastructure.Automation.TriggerIndexedRuleIndex."""

from __future__ import annotations

from winspark.domain.automation import AutomationRuleDefinition


class TriggerIndexedRuleIndex:
    def __init__(self) -> None:
        self._by_trigger: dict[str, list[AutomationRuleDefinition]] = {}
        self._all_enabled: list[AutomationRuleDefinition] = []

    def rebuild(self, rules: list[AutomationRuleDefinition]) -> None:
        enabled = [r for r in rules if r.is_enabled]
        index: dict[str, list[AutomationRuleDefinition]] = {}

        for rule in enabled:
            trigger_id = rule.trigger.type_id.lower()
            index.setdefault(trigger_id, []).append(rule)

        self._by_trigger = index
        self._all_enabled = enabled

    def get_rules_for_trigger(self, trigger_type_id: str) -> list[AutomationRuleDefinition]:
        return list(self._by_trigger.get(trigger_type_id.lower(), []))

    def get_all_enabled(self) -> list[AutomationRuleDefinition]:
        return list(self._all_enabled)

    def invalidate(self) -> None:
        self._by_trigger.clear()
        self._all_enabled.clear()
