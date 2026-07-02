"""Minimal ports of IEventRepository / IApplicationRepository / IApplicationSnapshotRepository."""

from __future__ import annotations

from datetime import datetime, timezone

from winspark.data.connection import ConnectionFactory
from winspark.domain.entities import (
    ApplicationEntity,
    ApplicationSnapshotEntity,
    AuditTrailEntity,
    AutomationRuleEntity,
    EventEntity,
    LogEntity,
    SettingEntity,
)
from winspark.domain.enums import AuditResultKind


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class EventRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def insert(self, event: EventEntity) -> int:
        conn = self._factory.create_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO Events (EventType, ProcessName, ProcessId, WindowHandle, WindowTitle, Details, OccurredAtUtc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(event.event_type),
                    event.process_name,
                    event.process_id,
                    event.window_handle,
                    event.window_title,
                    event.details,
                    _iso(event.occurred_at_utc),
                ),
            )
            return cursor.lastrowid
        finally:
            conn.close()

    def get_recent(self, count: int = 100) -> list[EventEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM Events ORDER BY OccurredAtUtc DESC LIMIT ?", (count,)
            ).fetchall()
            return [
                EventEntity(
                    id=row["Id"],
                    event_type=row["EventType"],
                    process_name=row["ProcessName"],
                    process_id=row["ProcessId"],
                    window_handle=row["WindowHandle"],
                    window_title=row["WindowTitle"],
                    details=row["Details"],
                    occurred_at_utc=datetime.fromisoformat(row["OccurredAtUtc"]),
                )
                for row in rows
            ]
        finally:
            conn.close()


class ApplicationRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def upsert(self, application: ApplicationEntity) -> int:
        conn = self._factory.create_connection()
        try:
            now = _iso(datetime.now(timezone.utc))
            conn.execute(
                """
                INSERT INTO Applications (ProcessName, FriendlyName, ExecutablePath, FirstSeenUtc, LastSeenUtc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ProcessName) DO UPDATE SET
                    FriendlyName = excluded.FriendlyName,
                    ExecutablePath = excluded.ExecutablePath,
                    LastSeenUtc = excluded.LastSeenUtc
                """,
                (application.process_name, application.friendly_name, application.executable_path, now, now),
            )
            row = conn.execute(
                "SELECT Id FROM Applications WHERE ProcessName = ?", (application.process_name,)
            ).fetchone()
            return row["Id"]
        finally:
            conn.close()


class ApplicationSnapshotRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def insert(self, snapshot: ApplicationSnapshotEntity) -> int:
        conn = self._factory.create_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO ApplicationSnapshots
                    (ApplicationId, WindowHandle, WindowTitle, ProcessName, ProcessId,
                     MemoryBytes, CpuPercent, WindowState, IsActive, CapturedAtUtc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.application_id,
                    snapshot.window_handle,
                    snapshot.window_title,
                    snapshot.process_name,
                    snapshot.process_id,
                    snapshot.memory_bytes,
                    snapshot.cpu_percent,
                    int(snapshot.window_state),
                    int(snapshot.is_active),
                    _iso(snapshot.captured_at_utc),
                ),
            )
            return cursor.lastrowid
        finally:
            conn.close()


class SettingsRepository:
    """Port of ISettingsRepository."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def get_value(self, key: str) -> str | None:
        conn = self._factory.create_connection()
        try:
            row = conn.execute("SELECT Value FROM Settings WHERE Key = ?", (key,)).fetchone()
            return row["Value"] if row else None
        finally:
            conn.close()

    def set_value(self, key: str, value: str) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                """
                INSERT INTO Settings (Key, Value, UpdatedAtUtc) VALUES (?, ?, ?)
                ON CONFLICT(Key) DO UPDATE SET Value = excluded.Value, UpdatedAtUtc = excluded.UpdatedAtUtc
                """,
                (key, value, _iso(datetime.now(timezone.utc))),
            )
        finally:
            conn.close()

    def get_all(self) -> list[SettingEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute("SELECT * FROM Settings").fetchall()
            return [
                SettingEntity(key=row["Key"], value=row["Value"], updated_at_utc=datetime.fromisoformat(row["UpdatedAtUtc"]))
                for row in rows
            ]
        finally:
            conn.close()


class LogRepository:
    """Port of ILogRepository."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def insert(self, log: LogEntity) -> int:
        conn = self._factory.create_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO Logs (Level, Message, Source, Exception, TimestampUtc) VALUES (?, ?, ?, ?, ?)",
                (log.level, log.message, log.source, log.exception, _iso(log.timestamp_utc)),
            )
            return cursor.lastrowid
        finally:
            conn.close()

    def get_recent(self, count: int = 200) -> list[LogEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM Logs ORDER BY TimestampUtc DESC LIMIT ?", (count,)
            ).fetchall()
            return [
                LogEntity(
                    id=row["Id"],
                    level=row["Level"],
                    message=row["Message"],
                    source=row["Source"],
                    exception=row["Exception"],
                    timestamp_utc=datetime.fromisoformat(row["TimestampUtc"]),
                )
                for row in rows
            ]
        finally:
            conn.close()


class AuditTrailRepository:
    """Port of IAuditTrailRepository."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def insert(self, entry: AuditTrailEntity) -> int:
        conn = self._factory.create_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO AutomationAuditTrail
                    (RuleId, RuleName, TriggeredBy, ActionName, TargetWindowHandle,
                     TargetProcessName, TargetWindowTitle, ExecutedAtUtc, Result, Success, Details, ErrorMessage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.rule_id,
                    entry.rule_name,
                    entry.triggered_by,
                    entry.action_name,
                    entry.target_window_handle,
                    entry.target_process_name,
                    entry.target_window_title,
                    _iso(entry.executed_at_utc),
                    int(entry.result),
                    int(entry.success),
                    entry.details,
                    entry.error_message,
                ),
            )
            return cursor.lastrowid
        finally:
            conn.close()

    def get_recent(self, count: int = 200) -> list[AuditTrailEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM AutomationAuditTrail ORDER BY ExecutedAtUtc DESC LIMIT ?", (count,)
            ).fetchall()
            return [_row_to_audit_entity(row) for row in rows]
        finally:
            conn.close()

    def get_by_rule_id(self, rule_id: int, count: int = 50) -> list[AuditTrailEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM AutomationAuditTrail WHERE RuleId = ? ORDER BY ExecutedAtUtc DESC LIMIT ?",
                (rule_id, count),
            ).fetchall()
            return [_row_to_audit_entity(row) for row in rows]
        finally:
            conn.close()


def _row_to_audit_entity(row) -> AuditTrailEntity:
    return AuditTrailEntity(
        id=row["Id"],
        rule_id=row["RuleId"],
        rule_name=row["RuleName"],
        triggered_by=row["TriggeredBy"],
        action_name=row["ActionName"],
        target_window_handle=row["TargetWindowHandle"],
        target_process_name=row["TargetProcessName"],
        target_window_title=row["TargetWindowTitle"],
        executed_at_utc=datetime.fromisoformat(row["ExecutedAtUtc"]),
        result=AuditResultKind(row["Result"]),
        success=bool(row["Success"]),
        details=row["Details"],
        error_message=row["ErrorMessage"],
    )


class AutomationRuleRepository:
    """Port of IAutomationRuleRepository."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def insert(self, rule: AutomationRuleEntity) -> int:
        conn = self._factory.create_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO AutomationRules
                    (Name, Description, IsEnabled, TriggerTypeId, TriggerConfigJson,
                     ConditionsJson, ActionsJson, CreatedAtUtc, UpdatedAtUtc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.name,
                    rule.description,
                    int(rule.is_enabled),
                    rule.trigger_type_id,
                    rule.trigger_config_json,
                    rule.conditions_json,
                    rule.actions_json,
                    _iso(rule.created_at_utc),
                    _iso(rule.updated_at_utc),
                ),
            )
            return cursor.lastrowid
        finally:
            conn.close()

    def update(self, rule: AutomationRuleEntity) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                """
                UPDATE AutomationRules SET
                    Name = ?, Description = ?, IsEnabled = ?, TriggerTypeId = ?, TriggerConfigJson = ?,
                    ConditionsJson = ?, ActionsJson = ?, UpdatedAtUtc = ?
                WHERE Id = ?
                """,
                (
                    rule.name,
                    rule.description,
                    int(rule.is_enabled),
                    rule.trigger_type_id,
                    rule.trigger_config_json,
                    rule.conditions_json,
                    rule.actions_json,
                    _iso(rule.updated_at_utc),
                    rule.id,
                ),
            )
        finally:
            conn.close()

    def delete(self, rule_id: int) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute("DELETE FROM AutomationRules WHERE Id = ?", (rule_id,))
        finally:
            conn.close()

    def get_by_id(self, rule_id: int) -> AutomationRuleEntity | None:
        conn = self._factory.create_connection()
        try:
            row = conn.execute("SELECT * FROM AutomationRules WHERE Id = ?", (rule_id,)).fetchone()
            return _row_to_rule_entity(row) if row else None
        finally:
            conn.close()

    def get_all(self) -> list[AutomationRuleEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute("SELECT * FROM AutomationRules").fetchall()
            return [_row_to_rule_entity(row) for row in rows]
        finally:
            conn.close()

    def get_enabled(self) -> list[AutomationRuleEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute("SELECT * FROM AutomationRules WHERE IsEnabled = 1").fetchall()
            return [_row_to_rule_entity(row) for row in rows]
        finally:
            conn.close()

    def set_enabled(self, rule_id: int, enabled: bool) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                "UPDATE AutomationRules SET IsEnabled = ?, UpdatedAtUtc = ? WHERE Id = ?",
                (int(enabled), _iso(datetime.now(timezone.utc)), rule_id),
            )
        finally:
            conn.close()


def _row_to_rule_entity(row) -> AutomationRuleEntity:
    return AutomationRuleEntity(
        id=row["Id"],
        name=row["Name"],
        description=row["Description"],
        is_enabled=bool(row["IsEnabled"]),
        trigger_type_id=row["TriggerTypeId"],
        trigger_config_json=row["TriggerConfigJson"],
        conditions_json=row["ConditionsJson"],
        actions_json=row["ActionsJson"],
        created_at_utc=datetime.fromisoformat(row["CreatedAtUtc"]),
        updated_at_utc=datetime.fromisoformat(row["UpdatedAtUtc"]),
    )
