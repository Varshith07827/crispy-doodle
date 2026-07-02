"""Port of WinSpark.Infrastructure.Automation.AutomationEngine (IAutomationEngine)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from winspark.automation.registry import AutomationComponentRegistry
from winspark.automation.safety import AutomationSafetyPolicy
from winspark.data.repositories import AuditTrailRepository
from winspark.domain.automation import (
    AutomationActionDefinition,
    AutomationActionResult,
    AutomationContext,
    AutomationExecutionSummary,
    AutomationRuleDefinition,
)
from winspark.domain.entities import AuditTrailEntity

logger = logging.getLogger(__name__)


class AutomationEngine:
    def __init__(
        self,
        registry: AutomationComponentRegistry,
        safety_policy: AutomationSafetyPolicy,
        audit_trail: AuditTrailRepository,
    ) -> None:
        self._registry = registry
        self._safety_policy = safety_policy
        self._audit_trail = audit_trail
        self._rule_executed_handlers: list[Callable[[AutomationExecutionSummary], None]] = []

    def on_rule_executed(self, handler: Callable[[AutomationExecutionSummary], None]) -> None:
        self._rule_executed_handlers.append(handler)

    async def execute_rule_async(
        self, rule: AutomationRuleDefinition, context: AutomationContext
    ) -> AutomationExecutionSummary:
        results: list[AutomationActionResult] = []

        for action_def in rule.actions:
            result = await self.execute_action_async(context, action_def)
            results.append(result)
            self._record_audit(rule, context, action_def, result)

        summary = AutomationExecutionSummary(
            rule_id=rule.id,
            rule_name=rule.name,
            success=all(r.success for r in results),
            action_results=tuple(results),
            executed_at_utc=datetime.now(timezone.utc),
        )

        for handler in self._rule_executed_handlers:
            handler(summary)
        logger.info("Rule '%s' executed with success=%s", rule.name, summary.success)
        return summary

    async def execute_action_async(
        self, context: AutomationContext, definition: AutomationActionDefinition
    ) -> AutomationActionResult:
        action = self._registry.get_action(definition.type_id)
        if action is None:
            return AutomationActionResult.failed(f"Action '{definition.type_id}' is not registered.")

        safety = await self._safety_policy.evaluate_action_async(definition, context)
        if not safety.is_allowed:
            return AutomationActionResult.failed(safety.denial_reason or "Action blocked by safety policy.")

        try:
            return await action.execute_async(context, definition)
        except Exception as ex:  # noqa: BLE001
            logger.error("Action '%s' failed", definition.type_id, exc_info=True)
            return AutomationActionResult.failed(str(ex))

    def _record_audit(
        self,
        rule: AutomationRuleDefinition,
        context: AutomationContext,
        action_def: AutomationActionDefinition,
        result: AutomationActionResult,
    ) -> None:
        try:
            self._audit_trail.insert(
                AuditTrailEntity(
                    rule_id=rule.id or None,
                    rule_name=rule.name,
                    triggered_by=context.source,
                    action_name=action_def.type_id,
                    target_window_handle=context.window_handle,
                    target_process_name=context.process_name,
                    target_window_title=context.window_title,
                    executed_at_utc=datetime.now(timezone.utc),
                    result=result.result,
                    success=result.success,
                    details=result.message,
                    error_message=result.error_message,
                )
            )
        except Exception:  # noqa: BLE001
            logger.warning("Failed to record audit trail entry", exc_info=True)
