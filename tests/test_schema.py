from winspark.data.connection import ConnectionFactory
from winspark.data.repositories import ApplicationRepository, ApplicationSnapshotRepository, EventRepository
from winspark.domain.entities import ApplicationEntity, ApplicationSnapshotEntity, EventEntity
from winspark.domain.enums import EventTypeKind, WindowStateKind


def test_schema_creates_expected_tables(tmp_path):
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()

    conn = factory.create_connection()
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()

    expected = {
        "Applications",
        "ApplicationSnapshots",
        "Events",
        "Logs",
        "Settings",
        "AutomationRules",
        "AutomationAuditTrail",
        "Notifications",
        "ApplicationProfiles",
        "WhatsAppFetchBindings",
        "WhatsAppFetchRelayMessages",
    }
    assert expected <= tables


def test_event_repository_round_trip(tmp_path):
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()
    repo = EventRepository(factory)

    event_id = repo.insert(
        EventEntity(
            event_type=EventTypeKind.WINDOW_OPENED,
            process_name="notepad.exe",
            process_id=1234,
            window_handle=999,
            window_title="Untitled - Notepad",
        )
    )
    assert event_id > 0

    recent = repo.get_recent(count=10)
    assert len(recent) == 1
    assert recent[0].process_name == "notepad.exe"
    assert recent[0].event_type == EventTypeKind.WINDOW_OPENED


def test_application_upsert_is_idempotent_per_process_name(tmp_path):
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()
    repo = ApplicationRepository(factory)

    first_id = repo.upsert(ApplicationEntity(process_name="notepad.exe", friendly_name="Notepad"))
    second_id = repo.upsert(ApplicationEntity(process_name="notepad.exe", friendly_name="Notepad (updated)"))

    assert first_id == second_id


def test_snapshot_repository_insert(tmp_path):
    factory = ConnectionFactory(tmp_path / "test.db")
    factory.initialize_schema()
    app_repo = ApplicationRepository(factory)
    snapshot_repo = ApplicationSnapshotRepository(factory)

    app_id = app_repo.upsert(ApplicationEntity(process_name="notepad.exe", friendly_name="Notepad"))
    snapshot_id = snapshot_repo.insert(
        ApplicationSnapshotEntity(
            application_id=app_id,
            window_handle=999,
            window_title="Untitled - Notepad",
            process_name="notepad.exe",
            process_id=1234,
            window_state=WindowStateKind.NORMAL,
            is_active=True,
        )
    )
    assert snapshot_id > 0
