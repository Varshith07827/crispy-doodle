"""Mirrors WinSpark.Domain.Constants.WinSparkConstants (core subset)."""

APPLICATION_NAME = "winSpark"
DATABASE_FILE_NAME = "winspark.db"

DEFAULT_DISCOVERY_INTERVAL_SECONDS = 2.0
RECENT_EVENTS_CAPACITY = 500
SNAPSHOT_PERSIST_INTERVAL_SECONDS = 60
DEFAULT_RETENTION_DAYS = 30
SCHEMA_VERSION = 1  # base schema only; the .NET app is at 17 (AI/connector tables not yet ported)

MAX_RULE_EXECUTION_DEPTH = 8  # WinSparkConstants.MaxRuleExecutionDepth

SETTINGS_SAFETY_ALLOWLISTED_ACTIONS = "safety.allowlisted_actions"
SETTINGS_SAFETY_REQUIRE_DANGEROUS_CONFIRM = "safety.require_dangerous_confirm"
SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED = "whatsapp.fetch_relay.enabled"  # WinSparkConstants.SettingsWhatsAppFetchRelayEnabled

# App-wide AI configuration (one key/model/provider shared by every chat that
# replies via AI). Stored in the Settings table; per-chat prompt/mode live on
# the binding. The api_key/model setting names keep their original "openai.*"
# strings so existing saved values are preserved.
SETTINGS_OPENAI_API_KEY = "openai.api_key"
SETTINGS_OPENAI_MODEL = "openai.model"
SETTINGS_AI_PROVIDER = "ai.provider"
# How the "Do it" agent executes plans: "ask_risky" pauses for approval when a
# plan contains a risky step (send/delete/pay/...); "auto" runs immediately.
SETTINGS_AGENT_MODE = "agent.mode"
DEFAULT_AGENT_MODE = "ask_risky"

# Master switch for the automation trigger runner. When paused, no automation
# fires on its own (schedule or screen) — manual "Run now" still works.
SETTINGS_AUTOMATIONS_PAUSED = "automations.paused"

# Apps the user has pinned (JSON list of {display_name, exe_path, process_name}).
# Pinned apps show in the automation app pickers even when not currently open,
# and can be launched automatically when an automation that targets them runs.
SETTINGS_PINNED_APPS = "apps.pinned"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_AI_PROVIDER = "openai"

# AI providers with an OpenAI-compatible /chat/completions API. Groq speaks the
# same protocol as OpenAI, so the same client works against either — only the
# base URL and default model differ.
AI_PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
    },
}


def ai_provider_info(provider: str) -> dict[str, str]:
    """Base URL + default model + label for a provider key, falling back to the
    default provider for anything unrecognized."""
    return AI_PROVIDERS.get((provider or "").strip().lower(), AI_PROVIDERS[DEFAULT_AI_PROVIDER])


class AutomationTypeIds:
    """Port of WinSpark.Domain.Constants.AutomationTypeIds."""

    # Triggers
    TRIGGER_WINDOW_OPENED = "trigger.window.opened"
    TRIGGER_WINDOW_CLOSED = "trigger.window.closed"
    TRIGGER_WINDOW_ACTIVATED = "trigger.window.activated"
    TRIGGER_WINDOW_TITLE_CHANGED = "trigger.window.title_changed"
    TRIGGER_PROCESS_STARTED = "trigger.process.started"
    TRIGGER_PROCESS_EXITED = "trigger.process.exited"
    TRIGGER_NOTIFICATION_RECEIVED = "trigger.notification.received"

    # Conditions
    CONDITION_ALWAYS = "condition.always"
    CONDITION_PROCESS_NAME_EQUALS = "condition.process.name_equals"
    CONDITION_PROCESS_NAME_CONTAINS = "condition.process.name_contains"
    CONDITION_WINDOW_TITLE_CONTAINS = "condition.window.title_contains"
    CONDITION_WINDOW_TITLE_EQUALS = "condition.window.title_equals"

    # Actions
    ACTION_LOG_EVENT = "action.log"
    ACTION_SHOW_NOTIFICATION = "action.show_notification"
    ACTION_EXECUTE_RULE = "action.execute_rule"
    ACTION_BRING_TO_FRONT = "action.window.bring_to_front"
    ACTION_ACTIVATE_WINDOW = "action.window.activate"
    ACTION_MINIMIZE_WINDOW = "action.window.minimize"
    ACTION_MAXIMIZE_WINDOW = "action.window.maximize"
    ACTION_RESTORE_WINDOW = "action.window.restore"
    ACTION_CLOSE_WINDOW = "action.window.close"
    ACTION_INJECT_TEXT = "action.text.inject"
    ACTION_CONNECTOR_SEND = "action.connector.send"


class BusEventTypes:
    """Port of WinSpark.Domain.Constants.BusEventTypes."""

    WINDOW_OPENED = "window.opened"
    WINDOW_CLOSED = "window.closed"
    WINDOW_ACTIVATED = "window.activated"
    WINDOW_TITLE_CHANGED = "window.title_changed"
    PROCESS_STARTED = "process.started"
    PROCESS_EXITED = "process.exited"
    NOTIFICATION_RECEIVED = "notification.received"
    RULE_EXECUTED = "automation.rule_executed"
    ACTION_EXECUTED = "automation.action_executed"
