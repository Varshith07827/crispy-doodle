"""Ports of WinSpark.Domain.Entities.* — rows persisted to SQLite (core subset)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from winspark.domain.enums import AuditResultKind, EventTypeKind, WindowStateKind


@dataclass(slots=True)
class EventEntity:
    event_type: EventTypeKind
    process_name: str = ""
    process_id: int = 0
    window_handle: int = 0
    window_title: str = ""
    details: str = ""
    occurred_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: Optional[int] = None


@dataclass(slots=True)
class ApplicationEntity:
    process_name: str
    friendly_name: str
    executable_path: str = ""
    first_seen_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: Optional[int] = None


@dataclass(slots=True)
class ApplicationSnapshotEntity:
    window_handle: int
    window_title: str
    process_name: str
    process_id: int
    application_id: Optional[int] = None
    memory_bytes: int = 0
    cpu_percent: float = 0.0
    window_state: WindowStateKind = WindowStateKind.NORMAL
    is_active: bool = False
    captured_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: Optional[int] = None


@dataclass(slots=True)
class LogEntity:
    level: str
    message: str
    source: str = ""
    exception: Optional[str] = None
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: Optional[int] = None


@dataclass(slots=True)
class SettingEntity:
    key: str
    value: str
    updated_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class AutomationRuleEntity:
    name: str = ""
    description: str = ""
    is_enabled: bool = True
    trigger_type_id: str = ""
    trigger_config_json: str = "{}"
    conditions_json: str = "[]"
    actions_json: str = "[]"
    created_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: Optional[int] = None


@dataclass(slots=True)
class AuditTrailEntity:
    rule_id: Optional[int] = None
    rule_name: str = ""
    triggered_by: str = ""
    action_name: str = ""
    target_window_handle: int = 0
    target_process_name: str = ""
    target_window_title: str = ""
    executed_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    result: AuditResultKind = AuditResultKind.SUCCESS
    success: bool = False
    details: str = ""
    error_message: Optional[str] = None
    id: Optional[int] = None
