"""Port of WinSpark.Domain.Models.{ControlLocator,TextInjectionRequest,TextInjectionResult,UiControlInfo}.

Kept separate from domain/automation.py (rule/action definitions) the same
way the .NET solution keeps these in the plain Models namespace rather than
Models.Automation — they describe UI Automation targets, not rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from winspark.domain.enums import ControlTypeKind, TextInjectionMode


@dataclass(frozen=True, slots=True)
class ControlLocator:
    window_handle: int = 0
    name: Optional[str] = None
    automation_id: Optional[str] = None
    class_name: Optional[str] = None
    control_type: Optional[ControlTypeKind] = None
    index: Optional[int] = None


@dataclass(frozen=True, slots=True)
class TextInjectionRequest:
    window_handle: int
    locator: ControlLocator = field(default_factory=ControlLocator)
    text: str = ""
    mode: TextInjectionMode = TextInjectionMode.INSERT
    send_enter_after: bool = False


@dataclass(frozen=True, slots=True)
class TextInjectionResult:
    success: bool = False
    error_message: Optional[str] = None
    applied_text: Optional[str] = None

    @staticmethod
    def succeeded(applied_text: Optional[str] = None) -> "TextInjectionResult":
        return TextInjectionResult(success=True, applied_text=applied_text)

    @staticmethod
    def failed(error: str) -> "TextInjectionResult":
        return TextInjectionResult(success=False, error_message=error)


@dataclass(frozen=True, slots=True)
class UiControlInfo:
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    control_type: ControlTypeKind = ControlTypeKind.UNKNOWN
    value: Optional[str] = None
    help_text: Optional[str] = None
    is_enabled: bool = False
    is_offscreen: bool = False
    children: tuple["UiControlInfo", ...] = ()
