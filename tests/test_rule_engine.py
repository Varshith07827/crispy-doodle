"""End-to-end test of the ported rule/automation pipeline: RuleEngine wired to
a real AutomationComponentRegistry (built-in triggers/conditions), a real
AutomationEngine + AutomationSafetyPolicy + LogEventAction, and a real SQLite
database — mirrors how app.py would wire it, exercised without pywin32 so it
runs on any platform.

Verifies: trigger-indexed dispatch fires rules on matching bus events, does
not fire on non-matching events, condition filtering excludes non-matching
rules, and RuleEngine.execute_rule_by_id_async enforces the depth limit and
cycle detection ported from RuleEngine.cs.
"""

from datetime import datetime, timezone

import pytest

from winspark.automation.actions import LogEventAction
from winspark.automation.engine import AutomationEngine
from winspark.automation.mapper import to_entity
from winspark.automation.registry import AutomationComponentRegistry, register_builtin_triggers_and_conditions
from winspark.automation.rule_engine import RuleEngine
from winspark.automation.rule_index import TriggerIndexedRuleIndex
from winspark.automation.safety import AutomationSafetyPolicy
from winspark.constants import AutomationTypeIds, BusEventTypes, MAX_RULE_EXECUTION_DEPTH
from winspark.data.connection import ConnectionFactory
from winspark.data.repositories import AuditTrailRepository, AutomationRuleRepository, LogRepository, SettingsRepository
from winspark.domain.automation import (
    AutomationActionDefinition,
    AutomationConditionDefinition,
    AutomationContext,
    AutomationRuleDefinition,
    AutomationTriggerDefinition,
)
from winspark.domain.entities import EventEntity
from winspark.domain.enums import BusEventCategory, EventTypeKind
from winspark.domain.models import BusEvent
from winspark.eventbus.bus import EventBus


def _build_stack(tmp_path):
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()

    registry = AutomationComponentRegistry()
    register_builtin_triggers_and_conditions(registry)
    registry.register_action(LogEventAction(LogRepository(factory)))

    automation_engine = AutomationEngine(registry, AutomationSafetyPolicy(SettingsRepository(factory)), AuditTrailRepository(factory))
    rule_repository = AutomationRuleRepository(factory)
    rule_engine = RuleEngine(registry, automation_engine, rule_repository, TriggerIndexedRuleIndex(), EventBus())
    return factory, rule_repository, rule_engine


def _window_opened_event(process_name: str = "chrome.exe", title: str = "New Tab") -> BusEvent:
    entity = EventEntity(event_type=EventTypeKind.WINDOW_OPENED, process_name=process_name, window_title=title, window_handle=1)
    return BusEvent(category=BusEventCategory.WINDOW, event_type=BusEventTypes.WINDOW_OPENED, payload=entity, source="test")


@pytest.mark.asyncio
async def test_rule_fires_on_matching_trigger_and_writes_log(tmp_path):
    factory, rule_repository, rule_engine = _build_stack(tmp_path)

    rule = AutomationRuleDefinition(
        name="Log Chrome opens",
        trigger=AutomationTriggerDefinition(type_id=AutomationTypeIds.TRIGGER_WINDOW_OPENED),
        actions=(AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_LOG_EVENT, parameters={"message": "chrome opened"}),),
    )
    rule_repository.insert(to_entity(rule))

    await rule_engine.evaluate_event_async(_window_opened_event())

    logs = LogRepository(factory).get_recent()
    assert any(log.message == "chrome opened" for log in logs)

    audit = AuditTrailRepository(factory).get_recent()
    assert len(audit) == 1
    assert audit[0].success is True
    assert audit[0].action_name == AutomationTypeIds.ACTION_LOG_EVENT


@pytest.mark.asyncio
async def test_rule_does_not_fire_on_non_matching_trigger(tmp_path):
    factory, rule_repository, rule_engine = _build_stack(tmp_path)

    rule = AutomationRuleDefinition(
        name="Log window closes",
        trigger=AutomationTriggerDefinition(type_id=AutomationTypeIds.TRIGGER_WINDOW_CLOSED),
        actions=(AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_LOG_EVENT),),
    )
    rule_repository.insert(to_entity(rule))

    await rule_engine.evaluate_event_async(_window_opened_event())

    assert LogRepository(factory).get_recent() == []


@pytest.mark.asyncio
async def test_disabled_rule_does_not_fire(tmp_path):
    factory, rule_repository, rule_engine = _build_stack(tmp_path)

    rule = AutomationRuleDefinition(
        name="Disabled rule",
        is_enabled=False,
        trigger=AutomationTriggerDefinition(type_id=AutomationTypeIds.TRIGGER_WINDOW_OPENED),
        actions=(AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_LOG_EVENT),),
    )
    rule_repository.insert(to_entity(rule))

    await rule_engine.evaluate_event_async(_window_opened_event())

    assert LogRepository(factory).get_recent() == []


@pytest.mark.asyncio
async def test_condition_filters_out_non_matching_process(tmp_path):
    factory, rule_repository, rule_engine = _build_stack(tmp_path)

    rule = AutomationRuleDefinition(
        name="Only for notepad",
        trigger=AutomationTriggerDefinition(type_id=AutomationTypeIds.TRIGGER_WINDOW_OPENED),
        conditions=(AutomationConditionDefinition(type_id=AutomationTypeIds.CONDITION_PROCESS_NAME_EQUALS, parameters={"processName": "notepad.exe"}),),
        actions=(AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_LOG_EVENT, parameters={"message": "notepad opened"}),),
    )
    rule_repository.insert(to_entity(rule))

    await rule_engine.evaluate_event_async(_window_opened_event(process_name="chrome.exe"))
    assert LogRepository(factory).get_recent() == []

    await rule_engine.evaluate_event_async(_window_opened_event(process_name="notepad.exe"))
    assert any(log.message == "notepad opened" for log in LogRepository(factory).get_recent())


@pytest.mark.asyncio
async def test_execute_rule_by_id_enforces_depth_limit(tmp_path):
    _, rule_repository, rule_engine = _build_stack(tmp_path)
    rule_id = rule_repository.insert(to_entity(AutomationRuleDefinition(name="Any rule", actions=(AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_LOG_EVENT),))))

    deep_context = AutomationContext(execution_depth=MAX_RULE_EXECUTION_DEPTH)
    summary = await rule_engine.execute_rule_by_id_async(rule_id, deep_context)

    assert summary is not None
    assert summary.success is False
    assert summary.rule_name == "Depth Limit"


@pytest.mark.asyncio
async def test_execute_rule_by_id_detects_cycles(tmp_path):
    _, rule_repository, rule_engine = _build_stack(tmp_path)
    rule_id = rule_repository.insert(to_entity(AutomationRuleDefinition(name="Any rule", actions=(AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_LOG_EVENT),))))

    context = AutomationContext(visited_rule_ids=frozenset({rule_id}))
    summary = await rule_engine.execute_rule_by_id_async(rule_id, context)

    assert summary is not None
    assert summary.success is False
    assert summary.rule_name == "Cycle Detected"


@pytest.mark.asyncio
async def test_execute_rule_by_id_returns_none_for_unknown_rule(tmp_path):
    _, _, rule_engine = _build_stack(tmp_path)
    summary = await rule_engine.execute_rule_by_id_async(999, AutomationContext())
    assert summary is None
