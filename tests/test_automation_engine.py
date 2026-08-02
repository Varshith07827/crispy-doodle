"""Verifies AutomationEngine orchestration: unregistered actions fail cleanly,
the safety policy blocks Dangerous actions (e.g. close-window) when
confirmation is required and no confirmation provider is registered, and
execute_rule_async's overall success is the AND of all action results."""

import pytest

from winspark.automation.actions import LogEventAction
from winspark.automation.engine import AutomationEngine
from winspark.automation.registry import AutomationComponentRegistry
from winspark.automation.safety import AutomationSafetyPolicy
from winspark.constants import AutomationTypeIds
from winspark.data.connection import ConnectionFactory
from winspark.data.repositories import AuditTrailRepository, LogRepository, SettingsRepository
from winspark.domain.automation import AutomationActionDefinition, AutomationContext, AutomationRuleDefinition


def _build(tmp_path):
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()
    registry = AutomationComponentRegistry()
    registry.register_action(LogEventAction(LogRepository(factory)))
    engine = AutomationEngine(registry, AutomationSafetyPolicy(SettingsRepository(factory)), AuditTrailRepository(factory))
    return factory, registry, engine


@pytest.mark.asyncio
async def test_unregistered_action_fails_without_raising(tmp_path):
    _, _, engine = _build(tmp_path)
    result = await engine.execute_action_async(AutomationContext(), AutomationActionDefinition(type_id="action.does_not_exist"))
    assert result.success is False
    assert "not registered" in result.error_message


@pytest.mark.asyncio
async def test_dangerous_action_blocked_without_confirmation_provider(tmp_path):
    _, registry, engine = _build(tmp_path)

    class _StubCloseAction:
        type_id = AutomationTypeIds.ACTION_CLOSE_WINDOW
        display_name = "Close Window"
        category = "Window Actions"

        async def execute_async(self, context, definition):
            raise AssertionError("should never execute — safety policy should block it first")

    registry.register_action(_StubCloseAction())

    result = await engine.execute_action_async(
        AutomationContext(), AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_CLOSE_WINDOW)
    )

    assert result.success is False
    assert "no confirmation provider" in result.error_message


@pytest.mark.asyncio
async def test_dangerous_action_allowed_when_confirmation_requirement_disabled(tmp_path):
    factory, registry, engine = _build(tmp_path)
    AutomationSafetyPolicy(SettingsRepository(factory)).set_require_dangerous_confirmation(False)

    executed = []

    class _StubCloseAction:
        type_id = AutomationTypeIds.ACTION_CLOSE_WINDOW
        display_name = "Close Window"
        category = "Window Actions"

        async def execute_async(self, context, definition):
            executed.append(True)
            from winspark.domain.automation import AutomationActionResult

            return AutomationActionResult.succeeded()

    registry.register_action(_StubCloseAction())

    result = await engine.execute_action_async(
        AutomationContext(), AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_CLOSE_WINDOW)
    )

    assert executed == [True]
    assert result.success is True


@pytest.mark.asyncio
async def test_execute_rule_success_is_and_of_all_action_results(tmp_path):
    _, registry, engine = _build(tmp_path)
    rule = AutomationRuleDefinition(
        id=1,
        name="Mixed rule",
        actions=(
            AutomationActionDefinition(type_id=AutomationTypeIds.ACTION_LOG_EVENT, parameters={"message": "ok"}),
            AutomationActionDefinition(type_id="action.does_not_exist"),
        ),
    )

    summary = await engine.execute_rule_async(rule, AutomationContext(rule=rule, source="test"))

    assert summary.success is False
    assert len(summary.action_results) == 2
    assert summary.action_results[0].success is True
    assert summary.action_results[1].success is False
