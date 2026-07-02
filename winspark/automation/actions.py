"""Port of WinSpark.Infrastructure.Automation.BuiltIn.BuiltInActions.

Ported fully: LogEventAction, ShowNotificationAction, ExecuteRuleAction, all
six WindowActionBase actions (bring-to-front/activate/minimize/maximize/
restore/close), and InjectTextAction (via TextInjectionEngine — real UI
Automation element-tree writes through the STA thread manager, see
PORT_NOTES.md for what was verified).

Deliberately NOT ported (left unregistered rather than stubbed):
ConnectorSendAction — needs the WinSpark.AI connector layer
(WhatsApp/Slack/Teams/Outlook, 77 files, not ported). Registering a stub
would silently no-op at rule-execution time instead of failing loudly at
rule-authoring time, so it's left out of the registry entirely.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from winspark.automation.matcher import extract_unread_count, passes_unread_gate
from winspark.constants import AutomationTypeIds, BusEventTypes
from winspark.data.repositories import LogRepository
from winspark.domain.automation import (
    AutomationActionDefinition,
    AutomationActionResult,
    AutomationComponentDescriptor,
    AutomationContext,
    AutomationParameterDescriptor,
)
from winspark.domain.entities import LogEntity
from winspark.domain.enums import TextInjectionMode, WindowActionKind
from winspark.domain.ui_automation import ControlLocator, TextInjectionRequest
from winspark.engines.text_injection import TextInjectionEngine
from winspark.engines.window_actions import WindowActionService

logger = logging.getLogger(__name__)


class LogEventAction:
    type_id = AutomationTypeIds.ACTION_LOG_EVENT
    display_name = "Log Event"
    category = "Logging"

    def __init__(self, log_repository: LogRepository) -> None:
        self._log_repository = log_repository

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            parameters=(
                AutomationParameterDescriptor(
                    name="message", display_name="Message", default_value="Automation rule executed"
                ),
            ),
        )

    async def execute_async(
        self, context: AutomationContext, definition: AutomationActionDefinition
    ) -> AutomationActionResult:
        message = definition.parameters.get("message") or (
            f"Automation: {context.event_type} — {context.process_name} — {context.window_title}"
        )

        self._log_repository.insert(
            LogEntity(level="Information", message=message, source="AutomationEngine", timestamp_utc=datetime.now(timezone.utc))
        )
        logger.info("Automation log action: %s", message)
        return AutomationActionResult.succeeded(message)


_GENERIC_MESSAGE_TEMPLATES = {
    "automation rule executed",
    "a new process was detected by winspark.",
    "notepad opened",
    "chrome started",
    "edge started",
    "browser started",
    "file explorer opened",
    "gmail tab active",
    "explorer workflow started",
    "consider focusing on one task",
    "notification received",
    "ai suggested automation",
    "whatsapp unread messages — review manually",
}

_APP_NAME_ALIASES = (
    ("whatsapp", "WhatsApp"),
    ("outlook", "Outlook"),
    ("chrome", "Chrome"),
    ("msedge", "Edge"),
    ("notepad", "Notepad"),
    ("explorer", "File Explorer"),
)


def _is_generic_message(message: str) -> bool:
    if not message or not message.strip():
        return True
    if message.strip().lower() in _GENERIC_MESSAGE_TEMPLATES:
        return True
    lowered = message.lower()
    return len(message) < 28 and (" opened" in lowered or " started" in lowered)


def _format_app_name(process_name: str) -> str:
    if not process_name or not process_name.strip():
        return "Application"
    name = process_name[:-4] if process_name.lower().endswith(".exe") else process_name
    lowered = name.lower()
    for needle, display in _APP_NAME_ALIASES:
        if needle in lowered:
            return display
    return name


class ShowNotificationAction:
    """Port of ShowNotificationAction.

    The .NET version uses Microsoft.Toolkit.Uwp.Notifications (WinRT toast
    API). pywin32 has no equivalent, so this uses a Shell_NotifyIcon balloon
    tip instead — same "best effort OS notification" intent, different
    mechanism. Unlike the rest of this port, the toast itself isn't unit
    tested (it's a visual side effect); the message-building logic and the
    unread-gate skip logic are.
    """

    type_id = AutomationTypeIds.ACTION_SHOW_NOTIFICATION
    display_name = "Show Notification"
    category = "Notifications"

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            parameters=(
                AutomationParameterDescriptor(name="title", display_name="Title", default_value="winSpark"),
                AutomationParameterDescriptor(name="message", display_name="Message", is_required=True),
            ),
        )

    async def execute_async(
        self, context: AutomationContext, definition: AutomationActionDefinition
    ) -> AutomationActionResult:
        if context.rule is not None and not passes_unread_gate(context.rule, context):
            return AutomationActionResult.succeeded("Skipped — no unread messages detected")

        message = self._build_message(context, definition)
        title = self._build_title(context, definition)

        if "unread" in message.lower() and extract_unread_count(context.window_title) == 0:
            return AutomationActionResult.succeeded("Skipped — no unread messages in window title")

        try:
            _show_balloon_notification(title, message)
            return AutomationActionResult.succeeded(f"Notification shown: {message}")
        except Exception as ex:  # noqa: BLE001
            logger.warning("Failed to show toast notification: %s — %s", title, message, exc_info=True)
            return AutomationActionResult.failed(str(ex))

    @staticmethod
    def _build_title(context: AutomationContext, definition: AutomationActionDefinition) -> str:
        custom = definition.parameters.get("title")
        if custom and custom.strip():
            return custom
        if context.rule is not None and context.rule.name.strip():
            return context.rule.name
        return "winSpark"

    @staticmethod
    def _build_message(context: AutomationContext, definition: AutomationActionDefinition) -> str:
        custom = definition.parameters.get("message")
        if custom and custom.strip() and not _is_generic_message(custom):
            return custom

        app = _format_app_name(context.process_name)
        window = f" — {context.window_title}" if context.window_title.strip() else ""
        rule = f" ({context.rule.name})" if context.rule is not None and context.rule.name.strip() else ""

        if context.event_type.strip():
            return {
                BusEventTypes.WINDOW_OPENED: f"{app} window opened{window}{rule}",
                BusEventTypes.WINDOW_CLOSED: f"{app} window closed{window}{rule}",
                BusEventTypes.WINDOW_ACTIVATED: f"{app} is now active{window}{rule}",
                BusEventTypes.WINDOW_TITLE_CHANGED: f"{app} context updated{window}{rule}",
                BusEventTypes.NOTIFICATION_RECEIVED: f"Captured notification from {app}: {context.window_title}",
                BusEventTypes.PROCESS_STARTED: f"{app} process started{rule}",
                BusEventTypes.PROCESS_EXITED: f"{app} process exited{rule}",
            }.get(context.event_type, f"{app}{window}{rule}")

        if context.source.strip():
            return f"{context.source}: {app}{window}"

        return f"Automation triggered for {app}{rule}" if not window else f"{app}{window}{rule}"


def _show_balloon_notification(title: str, message: str) -> None:
    import win32api
    import win32con
    import win32gui

    wc = win32gui.WNDCLASS()
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = "WinSparkNotifyIconWindow"
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error:
        pass  # already registered from a previous call

    hwnd = win32gui.CreateWindow(wc.lpszClassName, "winSpark", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
    try:
        icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP | win32gui.NIF_INFO
        nid = (hwnd, 0, flags, win32con.WM_USER + 20, icon, "winSpark", message, 200, title)
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
    finally:
        win32gui.DestroyWindow(hwnd)


class ExecuteRuleAction:
    """Port of ExecuteRuleAction. Takes a lazy getter for the RuleEngine
    (mirrors the .NET version's IServiceProvider.GetRequiredService<IRuleEngine>()
    call) since RuleEngine -> AutomationEngine -> this action -> RuleEngine
    would otherwise be a circular construction dependency."""

    type_id = AutomationTypeIds.ACTION_EXECUTE_RULE
    display_name = "Execute Rule"
    category = "Automation"

    def __init__(self, get_rule_engine: Callable[[], object]) -> None:
        self._get_rule_engine = get_rule_engine

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            parameters=(AutomationParameterDescriptor(name="ruleId", display_name="Rule ID", is_required=True, data_type="long"),),
        )

    async def execute_async(
        self, context: AutomationContext, definition: AutomationActionDefinition
    ) -> AutomationActionResult:
        from winspark.constants import MAX_RULE_EXECUTION_DEPTH

        rule_id_text = definition.parameters.get("ruleId")
        if rule_id_text is None or not rule_id_text.strip().lstrip("-").isdigit():
            return AutomationActionResult.failed("Invalid ruleId parameter.")
        rule_id = int(rule_id_text)

        if context.execution_depth >= MAX_RULE_EXECUTION_DEPTH:
            return AutomationActionResult.failed("Maximum rule execution depth reached.")
        if rule_id in context.visited_rule_ids:
            return AutomationActionResult.failed("Circular rule execution detected.")

        rule_engine = self._get_rule_engine()
        summary = await rule_engine.execute_rule_by_id_async(rule_id, context)
        if summary is None:
            return AutomationActionResult.failed(f"Rule {rule_id} not found.")

        return (
            AutomationActionResult.succeeded(f"Executed rule '{summary.rule_name}'")
            if summary.success
            else AutomationActionResult.failed(f"Rule '{summary.rule_name}' execution failed.")
        )


class _WindowActionBase:
    category = "Window Actions"

    def __init__(self, window_actions: WindowActionService, type_id: str, display_name: str, action_kind: WindowActionKind) -> None:
        self._window_actions = window_actions
        self.type_id = type_id
        self.display_name = display_name
        self._action_kind = action_kind

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(type_id=self.type_id, display_name=self.display_name, category=self.category)

    async def execute_async(
        self, context: AutomationContext, definition: AutomationActionDefinition
    ) -> AutomationActionResult:
        if context.window_handle == 0:
            return AutomationActionResult.failed("No target window handle available.")

        from winspark.domain.automation import WindowActionRequest

        result = await self._window_actions.execute_async(
            WindowActionRequest(window_handle=context.window_handle, action=self._action_kind)
        )
        return (
            AutomationActionResult.succeeded(self.display_name)
            if result.success
            else AutomationActionResult.failed(result.error_message or "Window action failed.")
        )


def _build_window_actions(window_actions: WindowActionService) -> list[_WindowActionBase]:
    return [
        _WindowActionBase(window_actions, AutomationTypeIds.ACTION_BRING_TO_FRONT, "Bring Window To Front", WindowActionKind.BRING_TO_FRONT),
        _WindowActionBase(window_actions, AutomationTypeIds.ACTION_ACTIVATE_WINDOW, "Activate Window", WindowActionKind.ACTIVATE),
        _WindowActionBase(window_actions, AutomationTypeIds.ACTION_MINIMIZE_WINDOW, "Minimize Window", WindowActionKind.MINIMIZE),
        _WindowActionBase(window_actions, AutomationTypeIds.ACTION_MAXIMIZE_WINDOW, "Maximize Window", WindowActionKind.MAXIMIZE),
        _WindowActionBase(window_actions, AutomationTypeIds.ACTION_RESTORE_WINDOW, "Restore Window", WindowActionKind.RESTORE),
        _WindowActionBase(window_actions, AutomationTypeIds.ACTION_CLOSE_WINDOW, "Close Window", WindowActionKind.CLOSE),
    ]


class InjectTextAction:
    """Port of InjectTextAction — the one action that needs real UI Automation
    element-tree writes (via TextInjectionEngine / StaAutomationThreadManager)."""

    type_id = AutomationTypeIds.ACTION_INJECT_TEXT
    display_name = "Inject Text"
    category = "Text Injection"

    def __init__(self, text_injection: TextInjectionEngine) -> None:
        self._text_injection = text_injection

    @property
    def descriptor(self) -> AutomationComponentDescriptor:
        return AutomationComponentDescriptor(
            type_id=self.type_id,
            display_name=self.display_name,
            category=self.category,
            parameters=(
                AutomationParameterDescriptor(name="text", display_name="Text", is_required=True),
                AutomationParameterDescriptor(name="automationId", display_name="Automation ID"),
                AutomationParameterDescriptor(name="name", display_name="Control Name"),
                AutomationParameterDescriptor(name="sendEnter", display_name="Send Enter After", data_type="bool", default_value="false"),
            ),
        )

    async def execute_async(
        self, context: AutomationContext, definition: AutomationActionDefinition
    ) -> AutomationActionResult:
        if context.window_handle == 0:
            return AutomationActionResult.failed("No target window handle.")

        text = definition.parameters.get("text")
        if text is None:
            return AutomationActionResult.failed("Text parameter is required.")

        automation_id = definition.parameters.get("automationId")
        name = definition.parameters.get("name")
        send_enter = (definition.parameters.get("sendEnter") or "").lower() == "true"

        # NOTE: send_enter_after is set on the request below but — matching the C#
        # original exactly — TextInjectionEngine never reads it, so this is a no-op.
        # This mirrors a real dormant field in the .NET app, not a Python omission;
        # see PORT_NOTES.md.
        result = await self._text_injection.inject_text_async(
            TextInjectionRequest(
                window_handle=context.window_handle,
                text=text,
                mode=TextInjectionMode.REPLACE,
                send_enter_after=send_enter,
                locator=ControlLocator(window_handle=context.window_handle, automation_id=automation_id, name=name),
            )
        )

        return (
            AutomationActionResult.succeeded(f"Injected text: {text}")
            if result.success
            else AutomationActionResult.failed(result.error_message or "Text injection failed.")
        )


def register_builtin_actions(
    registry,
    log_repository: LogRepository,
    window_actions: WindowActionService,
    get_rule_engine: Optional[Callable[[], object]] = None,
    text_injection: Optional[TextInjectionEngine] = None,
) -> None:
    registry.register_action(LogEventAction(log_repository))
    registry.register_action(ShowNotificationAction())
    for action in _build_window_actions(window_actions):
        registry.register_action(action)
    if get_rule_engine is not None:
        registry.register_action(ExecuteRuleAction(get_rule_engine))
    if text_injection is not None:
        registry.register_action(InjectTextAction(text_injection))
