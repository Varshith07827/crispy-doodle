"""Verifies the ported diff algorithm (EventMonitoringEngine.process_snapshot) matches the
.NET semantics: WindowOpened/Closed/Activated/TitleChanged + ProcessStarted/Exited."""

from datetime import datetime, timezone

import pytest

from winspark.data.connection import ConnectionFactory
from winspark.data.repositories import ApplicationRepository, ApplicationSnapshotRepository, EventRepository
from winspark.domain.enums import EventTypeKind
from winspark.domain.models import WindowDiscoverySnapshot, WindowInfo
from winspark.engines.event_monitoring import EventMonitoringEngine
from winspark.eventbus.bus import EventBus


class _StubDiscoveryEngine:
    """No real window discovery needed — process_snapshot() is fed snapshots directly."""

    def on_snapshot_updated(self, callback):
        pass


def _make_engine(tmp_path) -> EventMonitoringEngine:
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()
    return EventMonitoringEngine(
        discovery_engine=_StubDiscoveryEngine(),
        event_repository=EventRepository(factory),
        application_repository=ApplicationRepository(factory),
        snapshot_repository=ApplicationSnapshotRepository(factory),
        event_bus=EventBus(),
    )


def _snapshot(*windows: WindowInfo) -> WindowDiscoverySnapshot:
    active = next((w for w in windows if w.is_active), None)
    return WindowDiscoverySnapshot(windows=tuple(windows), active_window=active, captured_at_utc=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_new_window_emits_opened_and_activated(tmp_path):
    engine = _make_engine(tmp_path)
    engine.is_running = True

    window = WindowInfo(handle=1, title="Notepad", process_name="notepad.exe", process_id=100, is_active=True)
    await engine.process_snapshot(_snapshot(window))

    event_types = [e.event_type for e in engine.recent_events]
    assert EventTypeKind.WINDOW_OPENED in event_types
    assert EventTypeKind.PROCESS_STARTED in event_types
    assert EventTypeKind.WINDOW_ACTIVATED in event_types


@pytest.mark.asyncio
async def test_title_change_is_detected(tmp_path):
    engine = _make_engine(tmp_path)
    engine.is_running = True

    w1 = WindowInfo(handle=1, title="Untitled - Notepad", process_name="notepad.exe", process_id=100)
    await engine.process_snapshot(_snapshot(w1))

    w2 = WindowInfo(handle=1, title="report.txt - Notepad", process_name="notepad.exe", process_id=100)
    await engine.process_snapshot(_snapshot(w2))

    assert any(e.event_type == EventTypeKind.WINDOW_TITLE_CHANGED for e in engine.recent_events)


@pytest.mark.asyncio
async def test_window_and_process_disappearing_emits_closed_and_exited(tmp_path):
    engine = _make_engine(tmp_path)
    engine.is_running = True

    window = WindowInfo(handle=1, title="Notepad", process_name="notepad.exe", process_id=100)
    await engine.process_snapshot(_snapshot(window))

    await engine.process_snapshot(_snapshot())  # window closed, process exited

    event_types = [e.event_type for e in engine.recent_events]
    assert EventTypeKind.WINDOW_CLOSED in event_types
    assert EventTypeKind.PROCESS_EXITED in event_types


@pytest.mark.asyncio
async def test_stable_window_between_snapshots_emits_no_events(tmp_path):
    engine = _make_engine(tmp_path)
    engine.is_running = True

    window = WindowInfo(handle=1, title="Notepad", process_name="notepad.exe", process_id=100)
    await engine.process_snapshot(_snapshot(window))
    events_after_first = len(engine.recent_events)

    await engine.process_snapshot(_snapshot(window))
    assert len(engine.recent_events) == events_after_first
