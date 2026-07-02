"""Port of the .NET hosted-service startup (PluginHostedService/EngineHostedService order,
minus plugins/automation/retention which aren't ported yet).

Run with: python -m winspark.app
"""

from __future__ import annotations

import asyncio
import logging

from winspark.automation.actions import register_builtin_actions
from winspark.automation.engine import AutomationEngine
from winspark.automation.registry import AutomationComponentRegistry, register_builtin_triggers_and_conditions
from winspark.automation.rule_engine import RuleEngine
from winspark.automation.rule_index import TriggerIndexedRuleIndex
from winspark.automation.safety import AutomationSafetyPolicy
from winspark.automation.sta_thread_manager import StaAutomationThreadManager
from winspark.connectors.fetch_webhook_mock_server import WhatsAppFetchLocalMockServer
from winspark.connectors.fetch_webhook_relay_service import WhatsAppFetchRelayService
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository
from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler
from winspark.connectors.whatsapp import WhatsAppConnector
from winspark.connectors.whatsapp_group_sender import WhatsAppGroupSender
from winspark.data.connection import ConnectionFactory, default_database_path
from winspark.data.repositories import (
    ApplicationRepository,
    ApplicationSnapshotRepository,
    AuditTrailRepository,
    AutomationRuleRepository,
    EventRepository,
    LogRepository,
    SettingsRepository,
)
from winspark.domain.entities import EventEntity
from winspark.engines.event_monitoring import EventMonitoringEngine
from winspark.engines.text_injection import TextInjectionEngine
from winspark.engines.ui_automation_interaction import UiAutomationInteractionEngine
from winspark.engines.window_actions import WindowActionService
from winspark.engines.window_discovery import WindowDiscoveryEngine
from winspark.eventbus.bus import EventBus


def _simple_friendly_name(process_name: str, title: str, pid: int) -> str:
    """Stand-in for IApplicationNameFormatter — the real one has UWP package resolution logic
    (IUwpPackageResolver) not yet ported."""
    return process_name.removesuffix(".exe").capitalize()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db_path = default_database_path()
    connection_factory = ConnectionFactory(db_path)
    connection_factory.initialize_schema()
    logging.info("SQLite database initialized at %s", db_path)

    event_repository = EventRepository(connection_factory)
    application_repository = ApplicationRepository(connection_factory)
    snapshot_repository = ApplicationSnapshotRepository(connection_factory)
    event_bus = EventBus()

    discovery_engine = WindowDiscoveryEngine(name_formatter=_simple_friendly_name)
    monitoring_engine = EventMonitoringEngine(
        discovery_engine=discovery_engine,
        event_repository=event_repository,
        application_repository=application_repository,
        snapshot_repository=snapshot_repository,
        event_bus=event_bus,
    )

    def _print_event(event: EventEntity) -> None:
        print(f"[{event.event_type.name}] {event.process_name} ({event.process_id}) - {event.window_title}")

    monitoring_engine.on_event_detected(_print_event)

    # Rule/automation engine (RuleEngine.cs / AutomationEngine.cs port), including
    # InjectTextAction via the STA-threaded UI Automation stack — see PORT_NOTES.md
    # for what's verified vs. still WinSpark.AI-connector-only (ConnectorSendAction).
    registry = AutomationComponentRegistry()
    register_builtin_triggers_and_conditions(registry)
    automation_engine = AutomationEngine(
        registry,
        AutomationSafetyPolicy(SettingsRepository(connection_factory)),
        AuditTrailRepository(connection_factory),
    )
    rule_engine = RuleEngine(
        registry,
        automation_engine,
        AutomationRuleRepository(connection_factory),
        TriggerIndexedRuleIndex(),
        event_bus,
    )
    sta_manager = StaAutomationThreadManager()
    text_injection = TextInjectionEngine(UiAutomationInteractionEngine(sta_manager))
    register_builtin_actions(
        registry,
        LogRepository(connection_factory),
        WindowActionService(),
        get_rule_engine=lambda: rule_engine,
        text_injection=text_injection,
    )

    # Fetch-Webhook relay: poll an external GET URL (typically backed by an AI
    # service) and relay non-empty responses into a WhatsApp chat — this port's
    # "AI" integration point. Wired but left disabled by default (no bindings
    # exist yet, and set_relay_enabled_async is never called here) so nothing
    # polls or sends automatically; see PORT_NOTES.md for how to use it.
    whatsapp_connector = WhatsAppConnector(sta_manager)
    whatsapp_group_sender = WhatsAppGroupSender(whatsapp_connector, sta_manager)
    fetch_webhook_scheduler = FetchWebhookBindingScheduler()
    fetch_webhook_mock_server = WhatsAppFetchLocalMockServer()
    fetch_relay_service = WhatsAppFetchRelayService(
        WhatsAppFetchRelayRepository(connection_factory),
        LogRepository(connection_factory),
        whatsapp_group_sender,
        fetch_webhook_mock_server,
        fetch_webhook_scheduler,
    )

    await monitoring_engine.start()
    await discovery_engine.start()
    await rule_engine.start_async()

    logging.info(
        "Fetch-Webhook relay ready (disabled) — %d binding(s) on file",
        len(fetch_relay_service.get_bindings()),
    )
    print("winSpark (Python port) running. Press Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await discovery_engine.stop()
        await monitoring_engine.stop()
        await rule_engine.stop_async()
        fetch_webhook_scheduler.dispose()
        fetch_webhook_mock_server.stop()
        sta_manager.dispose()


if __name__ == "__main__":
    asyncio.run(main())
