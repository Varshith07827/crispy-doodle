"""Port of WinSpark.Infrastructure.Repositories.WhatsAppFetchRelayRepository."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from winspark.connectors.fetch_webhook_models import (
    WhatsAppFetchBindingEntity,
    WhatsAppFetchRelayMessageEntity,
    WhatsAppFetchRelayMessageState,
)
from winspark.data.connection import ConnectionFactory


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def compute_content_hash(message_text: str) -> str:
    return hashlib.sha256(message_text.strip().encode("utf-8")).hexdigest().upper()


class WhatsAppFetchRelayRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._factory = connection_factory

    def get_bindings(self) -> list[WhatsAppFetchBindingEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute("SELECT * FROM WhatsAppFetchBindings ORDER BY CreatedAtUtc, GroupName").fetchall()
            return [_row_to_binding(r) for r in rows]
        finally:
            conn.close()

    def get_binding(self, binding_id: str) -> Optional[WhatsAppFetchBindingEntity]:
        conn = self._factory.create_connection()
        try:
            row = conn.execute("SELECT * FROM WhatsAppFetchBindings WHERE BindingId = ?", (binding_id,)).fetchone()
            return _row_to_binding(row) if row else None
        finally:
            conn.close()

    def upsert_binding(self, binding: WhatsAppFetchBindingEntity) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                """
                INSERT INTO WhatsAppFetchBindings
                    (BindingId, GroupName, FetchUrl, ApiKey, PollIntervalSeconds, IsEnabled,
                     LastFetchUtc, LastFetchState, LastMessageReceivedUtc, LastSendUtc, TotalPolls, TotalSent,
                     LastError, ReplySource, AiMode, AiPrompt, TriggerText, ReplyText, CreatedAtUtc, UpdatedAtUtc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(BindingId) DO UPDATE SET
                    GroupName = excluded.GroupName,
                    FetchUrl = excluded.FetchUrl,
                    ApiKey = excluded.ApiKey,
                    PollIntervalSeconds = excluded.PollIntervalSeconds,
                    IsEnabled = excluded.IsEnabled,
                    ReplySource = excluded.ReplySource,
                    AiMode = excluded.AiMode,
                    AiPrompt = excluded.AiPrompt,
                    TriggerText = excluded.TriggerText,
                    ReplyText = excluded.ReplyText,
                    UpdatedAtUtc = excluded.UpdatedAtUtc
                """,
                (
                    binding.binding_id,
                    binding.group_name,
                    binding.fetch_url,
                    binding.api_key,
                    binding.poll_interval_seconds,
                    int(binding.is_enabled),
                    _iso(binding.last_fetch_utc) if binding.last_fetch_utc else None,
                    binding.last_fetch_state,
                    _iso(binding.last_message_received_utc) if binding.last_message_received_utc else None,
                    _iso(binding.last_send_utc) if binding.last_send_utc else None,
                    binding.total_polls,
                    binding.total_sent,
                    binding.last_error,
                    binding.reply_source,
                    binding.ai_mode,
                    binding.ai_prompt,
                    binding.trigger_text,
                    binding.reply_text,
                    _iso(binding.created_at_utc),
                    _iso(datetime.now(timezone.utc)),
                ),
            )
        finally:
            conn.close()

    def delete_binding(self, binding_id: str) -> None:
        conn = self._factory.create_connection()
        try:
            # Chat memory is PERSISTENT and deliberately NOT deleted here — it
            # outlives the automation so re-adding a chat later keeps its
            # context. Only the binding and its relay messages are removed.
            conn.execute("DELETE FROM WhatsAppFetchRelayMessages WHERE BindingId = ?", (binding_id,))
            conn.execute("DELETE FROM WhatsAppFetchBindings WHERE BindingId = ?", (binding_id,))
        finally:
            conn.close()

    # --- per-chat rolling memory (used by AI reply mode) -----------------

    def append_chat_memory(self, group_name: str, role: str, sender: str, text: str,
                           keep: int = 24) -> None:
        """Record one message in a chat's rolling memory ('them' = incoming,
        'me' = sent by winSpark) and trim the chat to its newest `keep` rows —
        per-chat recall stays bounded no matter how long the chat runs."""
        group = group_name.strip()
        if not group or not text.strip():
            return
        conn = self._factory.create_connection()
        try:
            conn.execute(
                "INSERT INTO WhatsAppChatMemory (GroupName, Role, Sender, MessageText, CreatedAtUtc) VALUES (?, ?, ?, ?, ?)",
                (group, role, sender or "", text.strip(), _iso(datetime.now(timezone.utc))),
            )
            conn.execute(
                """
                DELETE FROM WhatsAppChatMemory WHERE GroupName = ? AND Id NOT IN (
                    SELECT Id FROM WhatsAppChatMemory WHERE GroupName = ? ORDER BY Id DESC LIMIT ?
                )
                """,
                (group, group, max(1, keep)),
            )
        finally:
            conn.close()

    def get_chat_memory(self, group_name: str, limit: int = 24) -> list[tuple[str, str, str]]:
        """The chat's remembered messages, oldest first: (role, sender, text)."""
        conn = self._factory.create_connection()
        try:
            rows = conn.execute(
                "SELECT Role, Sender, MessageText FROM WhatsAppChatMemory WHERE GroupName = ? ORDER BY Id DESC LIMIT ?",
                (group_name.strip(), max(1, limit)),
            ).fetchall()
            return [(r[0], r[1], r[2]) for r in reversed(rows)]
        finally:
            conn.close()

    def clear_chat_memory(self, group_name: str) -> None:
        """Forget everything remembered for one chat (the UI's Clear button)."""
        conn = self._factory.create_connection()
        try:
            conn.execute("DELETE FROM WhatsAppChatMemory WHERE GroupName = ?", (group_name.strip(),))
        finally:
            conn.close()

    def get_chats_with_memory(self) -> list[tuple[str, int]]:
        """Every chat that has remembered messages, with its message count —
        newest-active first. Powers the memory viewer."""
        conn = self._factory.create_connection()
        try:
            rows = conn.execute(
                "SELECT GroupName, COUNT(*) FROM WhatsAppChatMemory GROUP BY GroupName ORDER BY MAX(Id) DESC"
            ).fetchall()
            return [(r[0], r[1]) for r in rows]
        finally:
            conn.close()

    def update_binding_status(
        self,
        binding_id: str,
        last_fetch_state: str,
        last_fetch_utc: Optional[datetime] = None,
        last_message_received_utc: Optional[datetime] = None,
        last_send_utc: Optional[datetime] = None,
        last_error: str = "",
    ) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                """
                UPDATE WhatsAppFetchBindings SET
                    LastFetchState = ?,
                    LastFetchUtc = COALESCE(?, LastFetchUtc),
                    LastMessageReceivedUtc = COALESCE(?, LastMessageReceivedUtc),
                    LastSendUtc = CASE WHEN ? = 'send-failed' THEN NULL ELSE COALESCE(?, LastSendUtc) END,
                    LastError = ?,
                    UpdatedAtUtc = ?
                WHERE BindingId = ?
                """,
                (
                    last_fetch_state,
                    _iso(last_fetch_utc) if last_fetch_utc else None,
                    _iso(last_message_received_utc) if last_message_received_utc else None,
                    last_fetch_state,
                    _iso(last_send_utc) if last_send_utc else None,
                    last_error,
                    _iso(datetime.now(timezone.utc)),
                    binding_id,
                ),
            )
        finally:
            conn.close()

    def increment_binding_poll_count(self, binding_id: str) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                "UPDATE WhatsAppFetchBindings SET TotalPolls = TotalPolls + 1, UpdatedAtUtc = ? WHERE BindingId = ?",
                (_iso(datetime.now(timezone.utc)), binding_id),
            )
        finally:
            conn.close()

    def increment_binding_sent_count(self, binding_id: str, sent_utc: datetime) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                """
                UPDATE WhatsAppFetchBindings
                SET TotalSent = TotalSent + 1, LastSendUtc = ?, UpdatedAtUtc = ?
                WHERE BindingId = ?
                """,
                (_iso(sent_utc), _iso(datetime.now(timezone.utc)), binding_id),
            )
        finally:
            conn.close()

    def set_binding_enabled(self, binding_id: str, enabled: bool) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                "UPDATE WhatsAppFetchBindings SET IsEnabled = ?, UpdatedAtUtc = ? WHERE BindingId = ?",
                (int(enabled), _iso(datetime.now(timezone.utc)), binding_id),
            )
        finally:
            conn.close()

    def insert_message(self, message: WhatsAppFetchRelayMessageEntity) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                """
                INSERT INTO WhatsAppFetchRelayMessages
                    (MessageId, BindingId, ExternalId, MessageText, ContentHash, State, FetchUtc, SentUtc,
                     NextRetryUtc, ParseStrategy, LastError, AttemptCount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _message_params(message),
            )
        finally:
            conn.close()

    def update_message(self, message: WhatsAppFetchRelayMessageEntity) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                """
                UPDATE WhatsAppFetchRelayMessages SET
                    State = ?, SentUtc = ?, NextRetryUtc = ?, ParseStrategy = ?,
                    LastError = ?, AttemptCount = ?, MessageText = ?
                WHERE MessageId = ?
                """,
                (
                    int(message.state),
                    _iso(message.sent_utc) if message.sent_utc else None,
                    _iso(message.next_retry_utc) if message.next_retry_utc else None,
                    message.parse_strategy,
                    message.last_error,
                    message.attempt_count,
                    message.message_text,
                    message.message_id,
                ),
            )
        finally:
            conn.close()

    def get_retryable_messages(self) -> list[WhatsAppFetchRelayMessageEntity]:
        conn = self._factory.create_connection()
        try:
            now = _iso(datetime.now(timezone.utc))
            rows = conn.execute(
                """
                SELECT * FROM WhatsAppFetchRelayMessages
                WHERE State IN (3, 4) AND AttemptCount < 3
                  AND (NextRetryUtc IS NULL OR NextRetryUtc <= ?)
                ORDER BY FetchUtc ASC
                """,
                (now,),
            ).fetchall()
            return [_row_to_message(r) for r in rows]
        finally:
            conn.close()

    def was_message_already_handled(self, binding_id: str, external_id: Optional[str], content_hash: str) -> bool:
        conn = self._factory.create_connection()
        try:
            if external_id and external_id.strip():
                row = conn.execute(
                    "SELECT 1 FROM WhatsAppFetchRelayMessages WHERE BindingId = ? AND ExternalId = ? AND State = 2 LIMIT 1",
                    (binding_id, external_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM WhatsAppFetchRelayMessages WHERE BindingId = ? AND ContentHash = ? AND State = 2 LIMIT 1",
                    (binding_id, content_hash),
                ).fetchone()
            return row is not None
        finally:
            conn.close()

    def has_in_flight_message(self, binding_id: str, external_id: Optional[str], content_hash: str) -> bool:
        conn = self._factory.create_connection()
        try:
            if external_id and external_id.strip():
                row = conn.execute(
                    "SELECT 1 FROM WhatsAppFetchRelayMessages WHERE BindingId = ? AND ExternalId = ? AND State IN (0, 1) LIMIT 1",
                    (binding_id, external_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM WhatsAppFetchRelayMessages WHERE BindingId = ? AND ContentHash = ? AND State IN (0, 1) LIMIT 1",
                    (binding_id, content_hash),
                ).fetchone()
            return row is not None
        finally:
            conn.close()

    def find_message(
        self, binding_id: str, external_id: Optional[str], content_hash: str
    ) -> Optional[WhatsAppFetchRelayMessageEntity]:
        conn = self._factory.create_connection()
        try:
            if external_id and external_id.strip():
                row = conn.execute(
                    """
                    SELECT * FROM WhatsAppFetchRelayMessages WHERE BindingId = ? AND ExternalId = ?
                    ORDER BY FetchUtc DESC LIMIT 1
                    """,
                    (binding_id, external_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM WhatsAppFetchRelayMessages WHERE BindingId = ? AND ContentHash = ?
                    ORDER BY FetchUtc DESC LIMIT 1
                    """,
                    (binding_id, content_hash),
                ).fetchone()
            return _row_to_message(row) if row else None
        finally:
            conn.close()

    def get_message_by_id(self, message_id: str) -> Optional[WhatsAppFetchRelayMessageEntity]:
        conn = self._factory.create_connection()
        try:
            row = conn.execute(
                "SELECT * FROM WhatsAppFetchRelayMessages WHERE MessageId = ? LIMIT 1", (message_id,)
            ).fetchone()
            return _row_to_message(row) if row else None
        finally:
            conn.close()

    def reset_message_for_retry(self, message_id: str) -> None:
        conn = self._factory.create_connection()
        try:
            conn.execute(
                "UPDATE WhatsAppFetchRelayMessages SET State = ?, SentUtc = NULL, LastError = '' WHERE MessageId = ?",
                (int(WhatsAppFetchRelayMessageState.PENDING), message_id),
            )
        finally:
            conn.close()

    def get_recent_messages(self, limit: int = 50) -> list[WhatsAppFetchRelayMessageEntity]:
        conn = self._factory.create_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM WhatsAppFetchRelayMessages ORDER BY FetchUtc DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
            return [_row_to_message(r) for r in rows]
        finally:
            conn.close()


def _message_params(message: WhatsAppFetchRelayMessageEntity) -> tuple:
    return (
        message.message_id,
        message.binding_id,
        message.external_id,
        message.message_text,
        message.content_hash,
        int(message.state),
        _iso(message.fetch_utc),
        _iso(message.sent_utc) if message.sent_utc else None,
        _iso(message.next_retry_utc) if message.next_retry_utc else None,
        message.parse_strategy,
        message.last_error,
        message.attempt_count,
    )


def _row_to_binding(row) -> WhatsAppFetchBindingEntity:
    return WhatsAppFetchBindingEntity(
        binding_id=row["BindingId"],
        group_name=row["GroupName"],
        fetch_url=row["FetchUrl"],
        api_key=row["ApiKey"],
        poll_interval_seconds=row["PollIntervalSeconds"],
        is_enabled=bool(row["IsEnabled"]),
        last_fetch_utc=_parse_dt(row["LastFetchUtc"]),
        last_fetch_state=row["LastFetchState"],
        last_message_received_utc=_parse_dt(row["LastMessageReceivedUtc"]),
        last_send_utc=_parse_dt(row["LastSendUtc"]),
        total_polls=row["TotalPolls"],
        total_sent=row["TotalSent"],
        last_error=row["LastError"],
        reply_source=_row_get(row, "ReplySource", "web") or "web",
        ai_mode=_row_get(row, "AiMode", "reply") or "reply",
        ai_prompt=_row_get(row, "AiPrompt", "") or "",
        trigger_text=_row_get(row, "TriggerText", "") or "",
        reply_text=_row_get(row, "ReplyText", "") or "",
        created_at_utc=_parse_dt(row["CreatedAtUtc"]),
        updated_at_utc=_parse_dt(row["UpdatedAtUtc"]),
    )


def _row_get(row, key: str, default):
    """Read a column that may be absent on databases migrated at a different
    time (sqlite3.Row raises on unknown keys rather than returning None)."""
    return row[key] if key in row.keys() else default


def _row_to_message(row) -> WhatsAppFetchRelayMessageEntity:
    return WhatsAppFetchRelayMessageEntity(
        message_id=row["MessageId"],
        binding_id=row["BindingId"],
        external_id=row["ExternalId"],
        message_text=row["MessageText"],
        content_hash=row["ContentHash"],
        state=WhatsAppFetchRelayMessageState(row["State"]),
        fetch_utc=_parse_dt(row["FetchUtc"]),
        sent_utc=_parse_dt(row["SentUtc"]),
        next_retry_utc=_parse_dt(row["NextRetryUtc"]),
        parse_strategy=row["ParseStrategy"],
        last_error=row["LastError"],
        attempt_count=row["AttemptCount"],
    )
