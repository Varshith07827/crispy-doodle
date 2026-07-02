"""Port of WinSpark.Domain.Interfaces.EventBus.IEventBus (simplified: no timeout/slow-handler metrics yet)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

from winspark.domain.enums import BusEventCategory
from winspark.domain.models import BusEvent

Handler = Callable[[BusEvent], Awaitable[None]]

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._type_handlers: dict[str | None, list[Handler]] = defaultdict(list)
        self._category_handlers: dict[BusEventCategory, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str | None, handler: Handler) -> Callable[[], None]:
        self._type_handlers[event_type].append(handler)
        return lambda: self._type_handlers[event_type].remove(handler)

    def subscribe_category(self, category: BusEventCategory, handler: Handler) -> Callable[[], None]:
        self._category_handlers[category].append(handler)
        return lambda: self._category_handlers[category].remove(handler)

    async def publish(self, event: BusEvent) -> None:
        handlers = list(self._type_handlers.get(event.event_type, ())) + list(
            self._type_handlers.get(None, ())
        ) + list(self._category_handlers.get(event.category, ()))

        for handler in handlers:
            try:
                await handler(event)
            except Exception:  # noqa: BLE001 - a bad subscriber must not break the bus
                logger.exception("Event bus handler failed for %s", event.event_type)
