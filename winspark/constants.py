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

# Webhook link testing: when on, POSTing text to a chat's inbox link
# auto-creates a WhatsApp automation for that chat (if none) and sends the
# queued messages through the relay pipeline. When off, POSTs are ignored.
SETTINGS_WEBHOOK_TESTING = "whatsapp.webhook_testing"
DEFAULT_WEBHOOK_TESTING = True

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

# AI response style — a person-friendly temperature. "precise" keeps replies
# and agent decisions deterministic; "balanced"/"creative" loosen them.
SETTINGS_AI_STYLE = "ai.style"
DEFAULT_AI_STYLE = "precise"
AI_STYLES: dict[str, float] = {"precise": 0.0, "balanced": 0.4, "creative": 0.8}

# Master switch for the automation trigger runner. When paused, no automation
# fires on its own (schedule or screen) — manual "Run now" still works.
SETTINGS_AUTOMATIONS_PAUSED = "automations.paused"
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
        # Same /chat/completions endpoint, but the model browses the web
        # server-side — used for replies/questions when web lookup is on.
        "search_model": "gpt-4o-mini-search-preview",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "search_model": "groq/compound-mini",
    },
}

# When on, AI replies and screen questions run on the provider's web-search
# model (with automatic fallback to the configured model if it fails), so
# answers about current events aren't stuck at the model's training cutoff.
SETTINGS_AI_WEB_SEARCH = "ai.web_search"
DEFAULT_AI_WEB_SEARCH = True


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
