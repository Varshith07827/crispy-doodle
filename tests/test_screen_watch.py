"""Tests for the generic screen watchers: repository roundtrip and the watch
service's poll/match/fire behavior, with fakes for the window finder, the
screen reader, and the WhatsApp sender — no real OCR, windows, or AI."""

import pytest

from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler
from winspark.connectors.screen_watch import (
    ScreenWatcherEntity,
    ScreenWatcherRepository,
    ScreenWatchService,
)
from winspark.data.connection import ConnectionFactory


@pytest.fixture
def repository(tmp_path):
    factory = ConnectionFactory(tmp_path / "watch.db")
    factory.initialize_schema()
    return ScreenWatcherRepository(factory)


class _Screens:
    """Fake window finder + screen reader with adjustable content."""

    def __init__(self):
        self.window_handle: int | None = 42
        self.text = ""
        self.read_calls = 0

    def find_window(self, process_name, title_hint):
        return self.window_handle

    def read_screen(self, handle):
        self.read_calls += 1
        return True, self.text


def _build(repository, screens, ai_config=None, send_whatsapp=None):
    scheduler = FetchWebhookBindingScheduler()
    service = ScreenWatchService(
        repository,
        scheduler,
        find_window=screens.find_window,
        read_screen=screens.read_screen,
        ai_config_provider=ai_config,
        send_whatsapp=send_whatsapp,
    )
    notifications: list[tuple[str, str]] = []
    activity: list[tuple[str, str, str]] = []
    service.on_notification(lambda title, body: notifications.append((title, body)))
    service.on_activity(lambda app, kind, detail: activity.append((app, kind, detail)))
    return service, scheduler, notifications, activity


def test_repository_roundtrip(repository):
    watcher = ScreenWatcherEntity(
        process_name="chrome.exe", app_display_name="Chrome",
        watch_text="Download complete", action_kind="whatsapp",
        whatsapp_chat="Family", whatsapp_message="It finished!",
    )
    repository.upsert_watcher(watcher)

    loaded = repository.get_watcher(watcher.watcher_id)
    assert loaded is not None
    assert loaded.process_name == "chrome.exe"
    assert loaded.watch_text == "Download complete"
    assert loaded.whatsapp_chat == "Family"
    assert loaded.is_enabled is True

    repository.set_enabled(watcher.watcher_id, False)
    assert repository.get_watcher(watcher.watcher_id).is_enabled is False

    repository.delete_watcher(watcher.watcher_id)
    assert repository.get_watcher(watcher.watcher_id) is None


@pytest.mark.asyncio
async def test_literal_match_fires_notification_and_one_shots(repository):
    screens = _Screens()
    service, scheduler, notifications, activity = _build(repository, screens)
    try:
        watcher = ScreenWatcherEntity(process_name="chrome.exe", app_display_name="Chrome", watch_text="Download complete")
        repository.upsert_watcher(watcher)

        screens.text = "Files   Downloads   report.pdf — Download complete   Settings"
        await service.poll_watcher_async(watcher.watcher_id)

        assert len(notifications) == 1
        assert "Chrome" in notifications[0][0]
        after = repository.get_watcher(watcher.watcher_id)
        assert after.is_enabled is False  # one-shot: paused itself
        assert after.status == "matched"
        assert any(kind == "watch_matched" for _, kind, _ in activity)
    finally:
        scheduler.dispose()


@pytest.mark.asyncio
async def test_no_match_keeps_watching(repository):
    screens = _Screens()
    service, scheduler, notifications, _ = _build(repository, screens)
    try:
        watcher = ScreenWatcherEntity(process_name="chrome.exe", app_display_name="Chrome", watch_text="Download complete")
        repository.upsert_watcher(watcher)

        screens.text = "Files   Downloads   report.pdf — 43% done   Settings"
        await service.poll_watcher_async(watcher.watcher_id)

        assert notifications == []
        after = repository.get_watcher(watcher.watcher_id)
        assert after.is_enabled is True
        assert after.status == "watching"
    finally:
        scheduler.dispose()


@pytest.mark.asyncio
async def test_app_not_open_sets_status_without_erroring(repository):
    screens = _Screens()
    screens.window_handle = None
    service, scheduler, notifications, _ = _build(repository, screens)
    try:
        watcher = ScreenWatcherEntity(process_name="chrome.exe", app_display_name="Chrome", watch_text="anything")
        repository.upsert_watcher(watcher)

        await service.poll_watcher_async(watcher.watcher_id)

        assert notifications == []
        assert repository.get_watcher(watcher.watcher_id).status == "app-not-open"
    finally:
        scheduler.dispose()


@pytest.mark.asyncio
async def test_whatsapp_action_sends_and_notifies(repository):
    sent: list[tuple[str, str]] = []

    async def send_whatsapp(chat, message):
        sent.append((chat, message))
        return True, "sent"

    screens = _Screens()
    service, scheduler, notifications, _ = _build(repository, screens, send_whatsapp=send_whatsapp)
    try:
        watcher = ScreenWatcherEntity(
            process_name="chrome.exe", app_display_name="Chrome",
            watch_text="Out for delivery", action_kind="whatsapp",
            whatsapp_chat="Family", whatsapp_message="Package is out for delivery!",
        )
        repository.upsert_watcher(watcher)

        screens.text = "Tracking: your order is Out for delivery today"
        await service.poll_watcher_async(watcher.watcher_id)

        assert sent == [("Family", "Package is out for delivery!")]
        assert len(notifications) == 1
        assert repository.get_watcher(watcher.watcher_id).status == "matched"
    finally:
        scheduler.dispose()


@pytest.mark.asyncio
async def test_whatsapp_send_failure_surfaces_and_still_one_shots(repository):
    async def send_whatsapp(chat, message):
        return False, "WhatsApp is not running."

    screens = _Screens()
    service, scheduler, notifications, activity = _build(repository, screens, send_whatsapp=send_whatsapp)
    try:
        watcher = ScreenWatcherEntity(
            process_name="chrome.exe", app_display_name="Chrome",
            watch_text="done", action_kind="whatsapp", whatsapp_chat="Family",
        )
        repository.upsert_watcher(watcher)

        screens.text = "the job is done"
        await service.poll_watcher_async(watcher.watcher_id)

        after = repository.get_watcher(watcher.watcher_id)
        assert after.is_enabled is False
        assert after.status == "error"
        assert len(notifications) == 1  # the failure is still surfaced to the user
        assert any(kind == "watch_error" for _, kind, _ in activity)
    finally:
        scheduler.dispose()


@pytest.mark.asyncio
async def test_unchanged_screen_skips_the_ai_call(repository, monkeypatch):
    """The AI matcher must only run when the screen's text changed — a 10s
    poll against a static screen must not bill per tick."""
    from winspark.connectors import screen_watch as sw

    classify_calls = []

    async def fake_classify(api_key, model, intent, message, base_url=""):
        classify_calls.append(message)
        return False

    monkeypatch.setattr(sw.openai_client, "classify_intent_match_async", fake_classify)

    screens = _Screens()
    service, scheduler, _, _ = _build(repository, screens, ai_config=lambda: ("sk-test", "m", "https://x"))
    try:
        watcher = ScreenWatcherEntity(process_name="chrome.exe", app_display_name="Chrome", watch_text="the delivery arriving")
        repository.upsert_watcher(watcher)

        screens.text = "same screen text with no literal match"
        await service.poll_watcher_async(watcher.watcher_id)
        await service.poll_watcher_async(watcher.watcher_id)
        await service.poll_watcher_async(watcher.watcher_id)

        assert screens.read_calls == 3       # every tick reads the screen…
        assert len(classify_calls) == 1      # …but the AI ran only once

        screens.text = "now the screen changed"
        await service.poll_watcher_async(watcher.watcher_id)
        assert len(classify_calls) == 2      # changed text -> AI consulted again
    finally:
        scheduler.dispose()


@pytest.mark.asyncio
async def test_semantic_match_via_ai_fires(repository, monkeypatch):
    from winspark.connectors import screen_watch as sw

    async def fake_classify(api_key, model, intent, message, base_url=""):
        return True

    monkeypatch.setattr(sw.openai_client, "classify_intent_match_async", fake_classify)

    screens = _Screens()
    service, scheduler, notifications, _ = _build(repository, screens, ai_config=lambda: ("sk-test", "m", "https://x"))
    try:
        # No literal overlap between watch text and screen text — only the
        # semantic (AI) path can match this.
        watcher = ScreenWatcherEntity(process_name="chrome.exe", app_display_name="Chrome", watch_text="the parcel arriving")
        repository.upsert_watcher(watcher)

        screens.text = "Status: courier will reach you within 10 minutes"
        await service.poll_watcher_async(watcher.watcher_id)

        assert len(notifications) == 1
        assert repository.get_watcher(watcher.watcher_id).status == "matched"
    finally:
        scheduler.dispose()
