"""Port of WinSpark.Domain.Interfaces.Automation.IAutomationComponentRegistry +
WinSpark.Infrastructure.Automation.AutomationComponentRegistry +
Automation/BuiltIn/BuiltInTriggers.cs + BuiltInConditions.cs.

Triggers/conditions/actions are plain objects exposing: type_id, display_name,
category, descriptor, and matches()/evaluate_async()/execute_async() — mirrors
the C# IAutomationTrigger/IAutomationCondition/IAutomationAction interfaces
without needing a formal Protocol (duck typing is enough here, same as the
rest of this port).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from winspark.constants import AutomationTypeIds, BusEventTypes
from winspark.domain.automation import (
    AutomationComponentDescriptor,
    AutomationConditionDefinition,
    AutomationContext,
    AutomationParameterDescriptor,
    AutomationTriggerDefinition,
)
from winspark.domain.models import BusEvent


@runtime_checkable
class AutomationTrigger(Protocol):
    type_id: str
    display_name: str
    category: str

    @property
    def descriptor(self) -> AutomationComponentDescriptor: ...

    def matches(self, bus_event: BusEvent, definition: AutomationTriggerDefinition) -> bool: ...


@runtime_checkable
class AutomationCondition(Protocol):
    type_id: str
    display_name: str
    category: str

    @property
    def descriptor(self) -> AutomationComponentDescriptor: ...

    async def evaluate_async(self, context: AutomationContext, definition: AutomationConditionDefinition) -> bool: ...


class _TriggerBase:
    def __init__(self, type_id: str, display_name: str, bus_event_type: str) -> None:
        self.type_id = type_id
        self.display_name = display_name
        self.category = "Window & Process"
        self._bus_event_type = bus_event_type

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            description=f"Fires when {self.display_name.lower()}.",
        )

    def matches(self, bus_event: BusEvent, definition: AutomationTriggerDefinition) -> bool:
        if definition.type_id.lower() != self.type_id.lower():
            return False
        return bus_event.event_type.lower() == self._bus_event_type.lower()


def _build_builtin_triggers() -> list[_TriggerBase]:
    return [
        _TriggerBase(AutomationTypeIds.TRIGGER_WINDOW_OPENED, "Window Opened", BusEventTypes.WINDOW_OPENED),
        _TriggerBase(AutomationTypeIds.TRIGGER_WINDOW_CLOSED, "Window Closed", BusEventTypes.WINDOW_CLOSED),
        _TriggerBase(AutomationTypeIds.TRIGGER_WINDOW_ACTIVATED, "Window Activated", BusEventTypes.WINDOW_ACTIVATED),
        _TriggerBase(
            AutomationTypeIds.TRIGGER_WINDOW_TITLE_CHANGED, "Window Title Changed", BusEventTypes.WINDOW_TITLE_CHANGED
        ),
        _TriggerBase(AutomationTypeIds.TRIGGER_PROCESS_STARTED, "Process Started", BusEventTypes.PROCESS_STARTED),
        _TriggerBase(AutomationTypeIds.TRIGGER_PROCESS_EXITED, "Process Exited", BusEventTypes.PROCESS_EXITED),
        _TriggerBase(
            AutomationTypeIds.TRIGGER_NOTIFICATION_RECEIVED,
            "Notification Received",
            BusEventTypes.NOTIFICATION_RECEIVED,
        ),
    ]


class _AlwaysTrueCondition:
    type_id = AutomationTypeIds.CONDITION_ALWAYS
    display_name = "Always (No Filter)"
    category = "Filters"

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            description="Always passes. Use when no additional filter is needed.",
        )

    async def evaluate_async(self, context: AutomationContext, definition: AutomationConditionDefinition) -> bool:
        return True


class _ProcessNameEqualsCondition:
    type_id = AutomationTypeIds.CONDITION_PROCESS_NAME_EQUALS
    display_name = "Process Name Equals"
    category = "Filters"

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            parameters=(
                AutomationParameterDescriptor(name="processName", display_name="Process Name", is_required=True),
            ),
        )

    async def evaluate_async(self, context: AutomationContext, definition: AutomationConditionDefinition) -> bool:
        expected = definition.parameters.get("processName")
        if expected is None:
            return False
        return context.process_name.lower() == expected.lower()


class _ProcessNameContainsCondition:
    type_id = AutomationTypeIds.CONDITION_PROCESS_NAME_CONTAINS
    display_name = "Process Name Contains"
    category = "Filters"

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            parameters=(AutomationParameterDescriptor(name="text", display_name="Contains Text", is_required=True),),
        )

    async def evaluate_async(self, context: AutomationContext, definition: AutomationConditionDefinition) -> bool:
        text = definition.parameters.get("text")
        if text is None:
            return False
        return text.lower() in context.process_name.lower()


class _WindowTitleContainsCondition:
    type_id = AutomationTypeIds.CONDITION_WINDOW_TITLE_CONTAINS
    display_name = "Window Title Contains"
    category = "Filters"

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            parameters=(AutomationParameterDescriptor(name="text", display_name="Contains Text", is_required=True),),
        )

    async def evaluate_async(self, context: AutomationContext, definition: AutomationConditionDefinition) -> bool:
        text = definition.parameters.get("text")
        if text is None:
            return False
        return text.lower() in context.window_title.lower()


class _WindowTitleEqualsCondition:
    type_id = AutomationTypeIds.CONDITION_WINDOW_TITLE_EQUALS
    display_name = "Window Title Equals"
    category = "Filters"

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            parameters=(AutomationParameterDescriptor(name="title", display_name="Window Title", is_required=True),),
        )

    async def evaluate_async(self, context: AutomationContext, definition: AutomationConditionDefinition) -> bool:
        title = definition.parameters.get("title")
        if title is None:
            return False
        return context.window_title.lower() == title.lower()


def _build_builtin_conditions() -> list[object]:
    return [
        _AlwaysTrueCondition(),
        _ProcessNameEqualsCondition(),
        _ProcessNameContainsCondition(),
        _WindowTitleContainsCondition(),
        _WindowTitleEqualsCondition(),
    ]


class AutomationComponentRegistry:
    """Port of AutomationComponentRegistry. Actions are registered separately
    (see winspark.automation.actions.register_builtin_actions) since they need
    repository/engine dependencies that triggers/conditions don't."""

    def __init__(self) -> None:
        self._triggers: dict[str, object] = {}
        self._conditions: dict[str, object] = {}
        self._actions: dict[str, object] = {}

    def register_trigger(self, trigger: object) -> None:
        self._triggers[trigger.type_id.lower()] = trigger

    def register_condition(self, condition: object) -> None:
        self._conditions[condition.type_id.lower()] = condition

    def register_action(self, action: object) -> None:
        self._actions[action.type_id.lower()] = action

    def get_trigger(self, type_id: str) -> object | None:
        return self._triggers.get(type_id.lower())

    def get_condition(self, type_id: str) -> object | None:
        return self._conditions.get(type_id.lower())

    def get_action(self, type_id: str) -> object | None:
        return self._actions.get(type_id.lower())

    @property
    def triggers(self) -> list[object]:
        return list(self._triggers.values())

    @property
    def conditions(self) -> list[object]:
        return list(self._conditions.values())

    @property
    def actions(self) -> list[object]:
        return list(self._actions.values())

    def get_trigger_descriptors(self) -> list[AutomationComponentDescriptor]:
        return sorted((t.descriptor for t in self._triggers.values()), key=lambda d: d.display_name)

    def get_condition_descriptors(self) -> list[AutomationComponentDescriptor]:
        return sorted((c.descriptor for c in self._conditions.values()), key=lambda d: d.display_name)

    def get_action_descriptors(self) -> list[AutomationComponentDescriptor]:
        return sorted((a.descriptor for a in self._actions.values()), key=lambda d: d.display_name)


def register_builtin_triggers_and_conditions(registry: AutomationComponentRegistry) -> None:
    for trigger in _build_builtin_triggers():
        registry.register_trigger(trigger)
    for condition in _build_builtin_conditions():
        registry.register_condition(condition)
