"""Port of WinSpark.Infrastructure.Engines.EventMonitoringEngine.

Diffs consecutive WindowDiscoverySnapshots to detect window/process
open/close/activate/title-change events, same algorithm as the .NET version.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from winspark.constants import RECENT_EVENTS_CAPACITY, SNAPSHOT_PERSIST_INTERVAL_SECONDS
from winspark.data.repositories import ApplicationRepository, ApplicationSnapshotRepository, EventRepository
from winspark.domain.entities import ApplicationEntity, ApplicationSnapshotEntity, EventEntity
from winspark.domain.enums import EventTypeKind
from winspark.domain.models import WindowDiscoverySnapshot, WindowInfo
from winspark.engines.window_discovery import WindowDiscoveryEngine
from winspark.eventbus.bus import EventBus
from winspark.eventbus.publisher import from_window_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _TrackedWindow:
    handle: int
    title: str
    process_name: str
    process_id: int
    was_active: bool


class EventMonitoringEngine:
    """Port of IEventMonitoringEngine / EventMonitoringEngine."""

    def __init__(
        self,
        discovery_engine: WindowDiscoveryEngine,
        event_repository: EventRepository,
        application_repository: ApplicationRepository,
        snapshot_repository: ApplicationSnapshotRepository,
        event_bus: EventBus,
    ) -> None:
        self._discovery_engine = discovery_engine
        self._event_repository = event_repository
        self._application_repository = application_repository
        self._snapshot_repository = snapshot_repository
        self._event_bus = event_bus

        self._recent_events: deque[EventEntity] = deque(maxlen=RECENT_EVENTS_CAPACITY)
        self._tracked_windows: dict[int, _TrackedWindow] = {}
        self._tracked_processes: set[int] = set()
        self._last_periodic_snapshot_utc: Optional[datetime] = None

        self.is_running = False
        self._on_event_detected: list[Callable[[EventEntity], None]] = []

    def on_event_detected(self, callback: Callable[[EventEntity], None]) -> None:
        self._on_event_detected.append(callback)

    @property
    def recent_events(self) -> list[EventEntity]:
        return list(reversed(self._recent_events))

    async def start(self) -> None:
        if self.is_running:
            return
        self._discovery_engine.on_snapshot_updated(self._on_snapshot_updated_sync)
        self.is_running = True
        logger.info("Event Monitoring Engine started")

    async def stop(self) -> None:
        self.is_running = False
        logger.info("Event Monitoring Engine stopped")

    def _on_snapshot_updated_sync(self, snapshot: WindowDiscoverySnapshot) -> None:
        # The discovery engine invokes this synchronously from its own asyncio task;
        # scheduling as a task keeps snapshot processing from blocking discovery.
        import asyncio

        asyncio.create_task(self.process_snapshot(snapshot))

    async def process_snapshot(self, snapshot: WindowDiscoverySnapshot) -> None:
        if not self.is_running:
            return

        current_windows = {w.handle: w for w in snapshot.windows}
        current_processes = {w.process_id for w in snapshot.windows}

        previous_windows = self._tracked_windows
        previous_processes = self._tracked_processes

        detected_events: list[EventEntity] = []
        event_window_handles: set[int] = set()

        for window in snapshot.windows:
            previous = previous_windows.get(window.handle)
            if previous is None:
                detected_events.append(self._create_event(EventTypeKind.WINDOW_OPENED, window))
                event_window_handles.add(window.handle)
            elif previous.title != window.title:
                detected_events.append(
                    self._create_event(
                        EventTypeKind.WINDOW_TITLE_CHANGED,
                        window,
                        f"Title changed from '{previous.title}' to '{window.title}'",
                    )
                )
                event_window_handles.add(window.handle)

            if window.is_active and (previous is None or not previous.was_active):
                detected_events.append(self._create_event(EventTypeKind.WINDOW_ACTIVATED, window))
                event_window_handles.add(window.handle)

        for previous in previous_windows.values():
            if previous.handle not in current_windows:
                detected_events.append(
                    EventEntity(
                        event_type=EventTypeKind.WINDOW_CLOSED,
                        process_name=previous.process_name,
                        process_id=previous.process_id,
                        window_handle=previous.handle,
                        window_title=previous.title,
                        details="Window no longer visible",
                    )
                )
                event_window_handles.add(previous.handle)

        for pid in current_processes - previous_processes:
            window = next((w for w in snapshot.windows if w.process_id == pid), None)
            if window is not None:
                detected_events.append(self._create_event(EventTypeKind.PROCESS_STARTED, window))
                event_window_handles.add(window.handle)

        for pid in previous_processes - current_processes:
            previous = next((w for w in previous_windows.values() if w.process_id == pid), None)
            detected_events.append(
                EventEntity(
                    event_type=EventTypeKind.PROCESS_EXITED,
                    process_name=previous.process_name if previous else "unknown.exe",
                    process_id=pid,
                    window_handle=previous.handle if previous else 0,
                    window_title=previous.title if previous else "",
                    details="Process no longer has visible windows",
                )
            )

        self._tracked_windows = {
            w.handle: _TrackedWindow(w.handle, w.title, w.process_name, w.process_id, w.is_active)
            for w in snapshot.windows
        }
        self._tracked_processes = current_processes

        for event in detected_events:
            await self._publish_event(event)

        if detected_events:
            related = [w for w in snapshot.windows if w.handle in event_window_handles]
            self._persist_snapshots(related, snapshot.captured_at_utc)
        elif self._should_persist_periodic_snapshot():
            sample = [w for w in snapshot.windows if w.is_active][:5]
            if sample:
                self._persist_snapshots(sample, snapshot.captured_at_utc)

    def _persist_snapshots(self, windows: list[WindowInfo], captured_at_utc: datetime) -> None:
        for window in windows:
            try:
                app_id = self._application_repository.upsert(
                    ApplicationEntity(
                        process_name=window.process_name,
                        friendly_name=window.friendly_name,
                        executable_path=window.executable_path,
                    )
                )
                self._snapshot_repository.insert(
                    ApplicationSnapshotEntity(
                        application_id=app_id,
                        window_handle=window.handle,
                        window_title=window.title,
                        process_name=window.process_name,
                        process_id=window.process_id,
                        memory_bytes=window.memory_bytes,
                        cpu_percent=window.cpu_percent,
                        window_state=window.window_state,
                        is_active=window.is_active,
                        captured_at_utc=captured_at_utc,
                    )
                )
            except Exception:  # noqa: BLE001
                logger.warning("Failed to persist snapshot for window %s", window.handle, exc_info=True)

    async def _publish_event(self, event: EventEntity) -> None:
        event.id = self._event_repository.insert(event)
        self._recent_events.append(event)

        logger.info(
            "Event detected: %s - %s (%s) - %s",
            event.event_type.name,
            event.process_name,
            event.process_id,
            event.window_title,
        )

        for callback in self._on_event_detected:
            callback(event)

        try:
            await self._event_bus.publish(from_window_event(event))
        except Exception:  # noqa: BLE001
            logger.warning("Failed to publish event to event bus", exc_info=True)

    def _should_persist_periodic_snapshot(self) -> bool:
        now = datetime.now(timezone.utc)
        if (
            self._last_periodic_snapshot_utc is not None
            and (now - self._last_periodic_snapshot_utc).total_seconds() < SNAPSHOT_PERSIST_INTERVAL_SECONDS
        ):
            return False
        self._last_periodic_snapshot_utc = now
        return True

    @staticmethod
    def _create_event(event_type: EventTypeKind, window: WindowInfo, details: str = "") -> EventEntity:
        return EventEntity(
            event_type=event_type,
            process_name=window.process_name,
            process_id=window.process_id,
            window_handle=window.handle,
            window_title=window.title,
            details=details,
        )
