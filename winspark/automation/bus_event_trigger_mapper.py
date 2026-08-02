"""Port of WinSpark.Infrastructure.Automation.BusEventTriggerMapper."""

from __future__ import annotations

from typing import Optional

from winspark.constants import AutomationTypeIds, BusEventTypes
from winspark.domain.models import BusEvent

_MAP: dict[str, str] = {
    BusEventTypes.WINDOW_OPENED: AutomationTypeIds.TRIGGER_WINDOW_OPENED,
    BusEventTypes.WINDOW_CLOSED: AutomationTypeIds.TRIGGER_WINDOW_CLOSED,
    BusEventTypes.WINDOW_ACTIVATED: AutomationTypeIds.TRIGGER_WINDOW_ACTIVATED,
    BusEventTypes.WINDOW_TITLE_CHANGED: AutomationTypeIds.TRIGGER_WINDOW_TITLE_CHANGED,
    BusEventTypes.PROCESS_STARTED: AutomationTypeIds.TRIGGER_PROCESS_STARTED,
    BusEventTypes.PROCESS_EXITED: AutomationTypeIds.TRIGGER_PROCESS_EXITED,
    BusEventTypes.NOTIFICATION_RECEIVED: AutomationTypeIds.TRIGGER_NOTIFICATION_RECEIVED,
}


def map_to_trigger_type_id(bus_event: BusEvent) -> Optional[str]:
    return _MAP.get(bus_event.event_type)
