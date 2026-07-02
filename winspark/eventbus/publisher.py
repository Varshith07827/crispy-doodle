"""Port of WinSpark.Infrastructure.EventBus.EventBusPublisher.

Maps an EventEntity into the BusEvent shape the rule engine listens for.
The EventType string must match winspark.constants.BusEventTypes exactly
(e.g. "window.opened", not the Python enum member name "WINDOW_OPENED") —
that string is what BusEventTriggerMapper looks up to find matching rules.
"""

from __future__ import annotations

from winspark.constants import BusEventTypes
from winspark.domain.entities import EventEntity
from winspark.domain.enums import BusEventCategory, EventTypeKind
from winspark.domain.models import BusEvent

_EVENT_TYPE_MAP: dict[EventTypeKind, str] = {
    EventTypeKind.WINDOW_OPENED: BusEventTypes.WINDOW_OPENED,
    EventTypeKind.WINDOW_CLOSED: BusEventTypes.WINDOW_CLOSED,
    EventTypeKind.WINDOW_ACTIVATED: BusEventTypes.WINDOW_ACTIVATED,
    EventTypeKind.WINDOW_TITLE_CHANGED: BusEventTypes.WINDOW_TITLE_CHANGED,
    EventTypeKind.PROCESS_STARTED: BusEventTypes.PROCESS_STARTED,
    EventTypeKind.PROCESS_EXITED: BusEventTypes.PROCESS_EXITED,
}


def _map_event_type(kind: EventTypeKind) -> str:
    return _EVENT_TYPE_MAP.get(kind, kind.name)


def from_window_event(entity: EventEntity) -> BusEvent:
    category = (
        BusEventCategory.PROCESS
        if entity.event_type in (EventTypeKind.PROCESS_STARTED, EventTypeKind.PROCESS_EXITED)
        else BusEventCategory.WINDOW
    )
    return BusEvent(
        category=category,
        event_type=_map_event_type(entity.event_type),
        payload=entity,
        source="EventMonitoringEngine",
        occurred_at_utc=entity.occurred_at_utc,
    )
