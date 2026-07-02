"""Port of WinSpark.Domain.Models.Automation.* + AutomationComponentDescriptor +
WindowActionRequest/Result (core subset needed for the rule/automation engines)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from winspark.domain.entities import EventEntity
from winspark.domain.enums import AuditResultKind, AutomationSafetyLevel, BusEventCategory, WindowActionKind
from winspark.domain.models import WindowInfo


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class AutomationTriggerDefinition:
    type_id: str = ""
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AutomationConditionDefinition:
    type_id: str = ""
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AutomationActionDefinition:
    type_id: str = ""
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AutomationRuleDefinition:
    id: int = 0
    name: str = ""
    description: str = ""
    is_enabled: bool = True
    trigger: AutomationTriggerDefinition = field(default_factory=AutomationTriggerDefinition)
    conditions: tuple[AutomationConditionDefinition, ...] = ()
    actions: tuple[AutomationActionDefinition, ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationContext:
    source: str = ""
    category: Optional[BusEventCategory] = None
    event_type: str = ""
    occurred_at_utc: datetime = field(default_factory=_utcnow)

    window_event: Optional[EventEntity] = None
    window: Optional[WindowInfo] = None
    notification: Optional[object] = None
    rule: Optional[AutomationRuleDefinition] = None
    execution_depth: int = 0
    visited_rule_ids: frozenset[int] = frozenset()

    @property
    def window_handle(self) -> int:
        if self.window is not None:
            return self.window.handle
        if self.window_event is not None:
            return self.window_event.window_handle
        return 0

    @property
    def process_name(self) -> str:
        if self.window is not None:
            return self.window.process_name
        if self.window_event is not None:
            return self.window_event.process_name
        if self.notification is not None:
            return getattr(self.notification, "application_name", "")
        return ""

    @property
    def window_title(self) -> str:
        if self.window is not None:
            return self.window.title
        if self.window_event is not None:
            return self.window_event.window_title
        if self.notification is not None:
            return getattr(self.notification, "title", "")
        return ""

    @property
    def process_id(self) -> int:
        if self.window is not None:
            return self.window.process_id
        if self.window_event is not None:
            return self.window_event.process_id
        return 0


@dataclass(frozen=True, slots=True)
class AutomationActionResult:
    success: bool = False
    result: AuditResultKind = AuditResultKind.SUCCESS
    message: str = ""
    error_message: Optional[str] = None
    metadata: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def succeeded(message: str = "") -> "AutomationActionResult":
        return AutomationActionResult(success=True, result=AuditResultKind.SUCCESS, message=message)

    @staticmethod
    def failed(error: str) -> "AutomationActionResult":
        return AutomationActionResult(
            success=False, result=AuditResultKind.FAILURE, message=error, error_message=error
        )


@dataclass(frozen=True, slots=True)
class AutomationExecutionSummary:
    rule_id: int = 0
    rule_name: str = ""
    success: bool = False
    action_results: tuple[AutomationActionResult, ...] = ()
    executed_at_utc: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class AutomationParameterDescriptor:
    name: str = ""
    display_name: str = ""
    data_type: str = "string"
    is_required: bool = False
    default_value: Optional[str] = None


@dataclass(frozen=True, slots=True)
class AutomationComponentDescriptor:
    type_id: str = ""
    display_name: str = ""
    category: str = ""
    description: Optional[str] = None
    parameters: tuple[AutomationParameterDescriptor, ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationSafetyDecision:
    is_allowed: bool = False
    requires_confirmation: bool = False
    level: AutomationSafetyLevel = AutomationSafetyLevel.MODERATE
    denial_reason: Optional[str] = None

    @staticmethod
    def allow(level: AutomationSafetyLevel, requires_confirmation: bool = False) -> "AutomationSafetyDecision":
        return AutomationSafetyDecision(is_allowed=True, level=level, requires_confirmation=requires_confirmation)

    @staticmethod
    def deny(reason: str, level: AutomationSafetyLevel) -> "AutomationSafetyDecision":
        return AutomationSafetyDecision(is_allowed=False, level=level, denial_reason=reason)


@dataclass(frozen=True, slots=True)
class WindowActionRequest:
    window_handle: int
    action: WindowActionKind
    x: Optional[int] = None
    y: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass(frozen=True, slots=True)
class WindowActionResult:
    success: bool = False
    error_message: Optional[str] = None

    @staticmethod
    def succeeded() -> "WindowActionResult":
        return WindowActionResult(success=True)

    @staticmethod
    def failed(error: str) -> "WindowActionResult":
        return WindowActionResult(success=False, error_message=error)
