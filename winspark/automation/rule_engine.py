"""Port of WinSpark.Infrastructure.Automation.RuleEngine (IRuleEngine)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Optional

from winspark.automation import mapper
from winspark.automation.bus_event_trigger_mapper import map_to_trigger_type_id
from winspark.automation.engine import AutomationEngine
from winspark.automation.matcher import matches_trigger_parameters, passes_unread_gate
from winspark.automation.registry import AutomationComponentRegistry
from winspark.automation.rule_index import TriggerIndexedRuleIndex
from winspark.constants import MAX_RULE_EXECUTION_DEPTH
from winspark.data.repositories import AutomationRuleRepository
from winspark.domain.automation import AutomationContext, AutomationExecutionSummary, AutomationRuleDefinition
from winspark.domain.entities import EventEntity
from winspark.domain.enums import BusEventCategory
from winspark.domain.models import BusEvent, WindowInfo
from winspark.eventbus.bus import EventBus

logger = logging.getLogger(__name__)


class RuleEngine:
    def __init__(
        self,
        registry: AutomationComponentRegistry,
        automation_engine: AutomationEngine,
        rule_repository: AutomationRuleRepository,
        rule_index: TriggerIndexedRuleIndex,
        event_bus: EventBus,
    ) -> None:
        self._registry = registry
        self._automation_engine = automation_engine
        self._rule_repository = rule_repository
        self._rule_index = rule_index
        self._event_bus = event_bus

        self._evaluation_lock = asyncio.Lock()
        self._index_built = False
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._is_running = False

        self._rule_executed_handlers: list[Callable[[AutomationExecutionSummary], None]] = []
        automation_engine.on_rule_executed(lambda summary: self._notify_rule_executed(summary))

    def on_rule_executed(self, handler: Callable[[AutomationExecutionSummary], None]) -> None:
        self._rule_executed_handlers.append(handler)

    def _notify_rule_executed(self, summary: AutomationExecutionSummary) -> None:
        for handler in self._rule_executed_handlers:
            handler(summary)

    @property
    def is_running(self) -> bool:
        return self._is_running

    async def start_async(self) -> None:
        if self._is_running:
            return
        self._unsubscribe = self._event_bus.subscribe(None, self._on_bus_event_async)
        self._is_running = True
        logger.info("Rule Engine started")

    async def stop_async(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._is_running = False
        logger.info("Rule Engine stopped")

    async def get_rules_async(self) -> list[AutomationRuleDefinition]:
        entities = self._rule_repository.get_all()
        return [mapper.to_definition(e) for e in entities]

    async def save_rule_async(self, rule: AutomationRuleDefinition) -> int:
        entity = mapper.to_entity(rule)
        if rule.id > 0:
            entity.id = rule.id
            self._rule_repository.update(entity)
            rule_id = rule.id
        else:
            rule_id = self._rule_repository.insert(entity)

        self._invalidate_rule_cache()
        return rule_id

    async def delete_rule_async(self, rule_id: int) -> None:
        self._rule_repository.delete(rule_id)
        self._invalidate_rule_cache()

    async def set_rule_enabled_async(self, rule_id: int, enabled: bool) -> None:
        self._rule_repository.set_enabled(rule_id, enabled)
        self._invalidate_rule_cache()

    async def evaluate_event_async(self, bus_event: BusEvent) -> None:
        async with self._evaluation_lock:
            await self._ensure_index_async()
            trigger_type_id = map_to_trigger_type_id(bus_event)
            rules = (
                self._rule_index.get_rules_for_trigger(trigger_type_id)
                if trigger_type_id is not None
                else self._rule_index.get_all_enabled()
            )
            context = self._build_context(bus_event)

            for rule in rules:
                if not await self._matches_rule_async(rule, bus_event, context):
                    continue

                visited = set(context.visited_rule_ids)
                if rule.id > 0:
                    visited.add(rule.id)

                execution_context = replace(
                    context,
                    rule=rule,
                    source=f"RuleEngine:{rule.name}",
                    execution_depth=context.execution_depth + 1,
                    visited_rule_ids=frozenset(visited),
                )

                await self._automation_engine.execute_rule_async(rule, execution_context)

    async def execute_rule_by_id_async(
        self, rule_id: int, context: AutomationContext
    ) -> Optional[AutomationExecutionSummary]:
        if context.execution_depth >= MAX_RULE_EXECUTION_DEPTH:
            logger.warning("Rule execution depth limit reached (%d)", context.execution_depth)
            return AutomationExecutionSummary(
                rule_id=rule_id, rule_name="Depth Limit", success=False, executed_at_utc=datetime.now(timezone.utc)
            )

        if rule_id in context.visited_rule_ids:
            logger.warning("Circular rule execution detected for rule %d", rule_id)
            return AutomationExecutionSummary(
                rule_id=rule_id, rule_name="Cycle Detected", success=False, executed_at_utc=datetime.now(timezone.utc)
            )

        entity = self._rule_repository.get_by_id(rule_id)
        if entity is None:
            return None

        rule = mapper.to_definition(entity)
        execution_context = replace(
            context,
            rule=rule,
            source=f"Manual:{rule.name}",
            execution_depth=context.execution_depth + 1,
            visited_rule_ids=frozenset(context.visited_rule_ids | {rule_id}),
        )

        return await self._automation_engine.execute_rule_async(rule, execution_context)

    async def _on_bus_event_async(self, bus_event: BusEvent) -> None:
        if not self._is_running:
            return
        try:
            await self.evaluate_event_async(bus_event)
        except Exception:  # noqa: BLE001
            logger.error("Rule evaluation failed for event %s", bus_event.event_type, exc_info=True)

    async def _ensure_index_async(self) -> None:
        if self._index_built:
            return
        entities = self._rule_repository.get_enabled()
        rules = [mapper.to_definition(e) for e in entities]
        self._rule_index.rebuild(rules)
        self._index_built = True

    def _invalidate_rule_cache(self) -> None:
        self._index_built = False
        self._rule_index.invalidate()

    async def _matches_rule_async(
        self, rule: AutomationRuleDefinition, bus_event: BusEvent, context: AutomationContext
    ) -> bool:
        trigger = self._registry.get_trigger(rule.trigger.type_id)
        if trigger is None or not trigger.matches(bus_event, rule.trigger):
            return False

        if not matches_trigger_parameters(rule.trigger, context):
            return False

        if not passes_unread_gate(rule, context):
            return False

        if not rule.conditions:
            return True

        for condition_def in rule.conditions:
            condition = self._registry.get_condition(condition_def.type_id)
            if condition is None:
                return False
            if not await condition.evaluate_async(context, condition_def):
                return False

        return True

    @staticmethod
    def _build_context(bus_event: BusEvent) -> AutomationContext:
        context = AutomationContext(
            source=bus_event.source,
            category=bus_event.category,
            event_type=bus_event.event_type,
            occurred_at_utc=bus_event.occurred_at_utc,
        )

        payload = bus_event.payload
        if isinstance(payload, EventEntity):
            return replace(context, window_event=payload)
        if isinstance(payload, WindowInfo):
            return replace(context, window=payload)
        if isinstance(payload, AutomationContext):
            return payload
        return context
