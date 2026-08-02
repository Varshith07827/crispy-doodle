"""Ports of WinSpark.Domain.Enums.* (core subset needed for window/event tracking)."""

from enum import IntEnum


class EventTypeKind(IntEnum):
    WINDOW_OPENED = 0
    WINDOW_CLOSED = 1
    WINDOW_ACTIVATED = 2
    WINDOW_TITLE_CHANGED = 3
    PROCESS_STARTED = 4
    PROCESS_EXITED = 5


class WindowStateKind(IntEnum):
    NORMAL = 0
    MINIMIZED = 1
    MAXIMIZED = 2
    HIDDEN = 3


class BusEventCategory(IntEnum):
    WINDOW = 0
    PROCESS = 1
    NOTIFICATION = 2
    AUTOMATION = 3
    SYSTEM = 4


class WindowActionKind(IntEnum):
    BRING_TO_FRONT = 0
    ACTIVATE = 1
    MINIMIZE = 2
    MAXIMIZE = 3
    RESTORE = 4
    CLOSE = 5
    MOVE = 6
    RESIZE = 7


class AutomationSafetyLevel(IntEnum):
    SAFE = 0
    MODERATE = 1
    DANGEROUS = 2


class AuditResultKind(IntEnum):
    SUCCESS = 0
    FAILURE = 1
    SKIPPED = 2
    PARTIAL = 3


class TextInjectionMode(IntEnum):
    INSERT = 0
    APPEND = 1
    REPLACE = 2
    CLEAR = 3


class ControlTypeKind(IntEnum):
    UNKNOWN = 0
    BUTTON = 1
    TEXT_BOX = 2
    LABEL = 3
    LIST = 4
    MENU = 5
    MENU_ITEM = 6
    COMBO_BOX = 7
    CHECK_BOX = 8
    RADIO_BUTTON = 9
    TREE = 10
    DATA_GRID = 11
    TAB = 12
    WINDOW = 13
    PANE = 14
    DOCUMENT = 15
    OTHER = 99
