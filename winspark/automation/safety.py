"""Port of WinSpark.Infrastructure.Automation.AutomationSafetyPolicy."""

from __future__ import annotations

import json
from typing import Awaitable, Callable, Optional

from winspark.constants import (
    SETTINGS_SAFETY_ALLOWLISTED_ACTIONS,
    SETTINGS_SAFETY_REQUIRE_DANGEROUS_CONFIRM,
    AutomationTypeIds,
)
from winspark.data.repositories import SettingsRepository
from winspark.domain.automation import AutomationActionDefinition, AutomationContext, AutomationSafetyDecision
from winspark.domain.enums import AutomationSafetyLevel

_ACTION_LEVELS: dict[str, AutomationSafetyLevel] = {
    AutomationTypeIds.ACTION_LOG_EVENT: AutomationSafetyLevel.SAFE,
    AutomationTypeIds.ACTION_SHOW_NOTIFICATION: AutomationSafetyLevel.SAFE,
    AutomationTypeIds.ACTION_EXECUTE_RULE: AutomationSafetyLevel.MODERATE,
    AutomationTypeIds.ACTION_BRING_TO_FRONT: AutomationSafetyLevel.MODERATE,
    AutomationTypeIds.ACTION_ACTIVATE_WINDOW: AutomationSafetyLevel.MODERATE,
    AutomationTypeIds.ACTION_MINIMIZE_WINDOW: AutomationSafetyLevel.MODERATE,
    AutomationTypeIds.ACTION_MAXIMIZE_WINDOW: AutomationSafetyLevel.MODERATE,
    AutomationTypeIds.ACTION_RESTORE_WINDOW: AutomationSafetyLevel.MODERATE,
    AutomationTypeIds.ACTION_INJECT_TEXT: AutomationSafetyLevel.MODERATE,
    AutomationTypeIds.ACTION_CONNECTOR_SEND: AutomationSafetyLevel.MODERATE,
    AutomationTypeIds.ACTION_CLOSE_WINDOW: AutomationSafetyLevel.DANGEROUS,
}

# Port of ISafetyConfirmationProvider — an async callable taking (action_type_id, target_description).
ConfirmationProvider = Callable[[str, str], Awaitable[bool]]


class AutomationSafetyPolicy:
    def __init__(self, settings: SettingsRepository, confirmation_provider: Optional[ConfirmationProvider] = None):
        self._settings = settings
        self._confirmation_provider = confirmation_provider

    def get_action_level(self, action_type_id: str) -> AutomationSafetyLevel:
        return _ACTION_LEVELS.get(action_type_id, AutomationSafetyLevel.MODERATE)

    async def evaluate_action_async(
        self, action: AutomationActionDefinition, context: AutomationContext
    ) -> AutomationSafetyDecision:
        level = self.get_action_level(action.type_id)
        allowlist = self.get_allowlisted_actions()

        if action.type_id.lower() in {a.lower() for a in allowlist}:
            return AutomationSafetyDecision.allow(level)

        if level in (AutomationSafetyLevel.SAFE, AutomationSafetyLevel.MODERATE):
            return AutomationSafetyDecision.allow(level)

        if not self.get_require_dangerous_confirmation():
            return AutomationSafetyDecision.allow(level)

        if self._confirmation_provider is None:
            return AutomationSafetyDecision.deny("Dangerous action blocked: no confirmation provider registered.", level)

        target = f"{context.process_name} — {context.window_title}"
        confirmed = await self._confirmation_provider(action.type_id, target)
        if confirmed:
            return AutomationSafetyDecision.allow(level, requires_confirmation=True)
        return AutomationSafetyDecision.deny("User declined dangerous action.", level)

    def get_allowlisted_actions(self) -> list[str]:
        value = self._settings.get_value(SETTINGS_SAFETY_ALLOWLISTED_ACTIONS)
        if not value or not value.strip():
            return []
        try:
            parsed = json.loads(value)
            return [str(v) for v in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def set_allowlisted_actions(self, action_type_ids: list[str]) -> None:
        self._settings.set_value(SETTINGS_SAFETY_ALLOWLISTED_ACTIONS, json.dumps(action_type_ids))

    def get_require_dangerous_confirmation(self) -> bool:
        value = self._settings.get_value(SETTINGS_SAFETY_REQUIRE_DANGEROUS_CONFIRM)
        return value is None or value.lower() == "true"

    def set_require_dangerous_confirmation(self, required: bool) -> None:
        self._settings.set_value(SETTINGS_SAFETY_REQUIRE_DANGEROUS_CONFIRM, "true" if required else "false")
