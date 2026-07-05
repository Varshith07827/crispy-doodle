"""Unit tests for the saved-automation persistence contract: how an Automation
maps to/from the generic AutomationRules row (a "manual" trigger + the action
details in ActionsJson), and the plain-English summary. Pure functions — no DB,
no engine, no Windows."""

import json

from datetime import datetime, timedelta

from winspark.ui.engine_host import (
    AUTOMATION_APP_ACTION,
    AUTOMATION_WHATSAPP,
    TRIGGER_SCHEDULE,
    TRIGGER_SCREEN,
    Automation,
    _automation_to_rule,
    _rule_to_automation,
    _schedule_is_due,
)
from winspark.domain.entities import AutomationRuleEntity


def _sched(mode="interval", minutes=30, time="09:00"):
    return Automation(
        1, "n", AUTOMATION_WHATSAPP, "F", "F", "hi", True,
        trigger_type=TRIGGER_SCHEDULE, schedule_mode=mode,
        interval_minutes=minutes, daily_time=time,
    )


def test_whatsapp_automation_round_trips():
    original = Automation(
        id=3, name="Morning ping", kind=AUTOMATION_WHATSAPP,
        target="Family", target_display="Family", instruction="Good morning!",
        enabled=True,
    )
    rule = _automation_to_rule(original)
    assert rule.trigger_type_id == "manual"
    assert json.loads(rule.actions_json)[0]["kind"] == AUTOMATION_WHATSAPP

    back = _rule_to_automation(rule)
    assert back == original


def test_app_action_automation_round_trips():
    original = Automation(
        id=9, name="Search flights", kind=AUTOMATION_APP_ACTION,
        target="chrome.exe", target_display="Chrome",
        instruction="search Google for cheap flights", enabled=False,
    )
    assert _rule_to_automation(_automation_to_rule(original)) == original


def test_rule_description_is_the_human_summary():
    rule = _automation_to_rule(Automation(
        None, "x", AUTOMATION_APP_ACTION, "chrome.exe", "Chrome", "do a thing"))
    assert rule.description == "In Chrome: do a thing"


def test_summary_wording():
    wa = Automation(1, "n", AUTOMATION_WHATSAPP, "Mum", "Mum", "hi there")
    assert wa.summary() == "Message “Mum”: hi there"
    app = Automation(1, "n", AUTOMATION_APP_ACTION, "chrome.exe", "Chrome", "search")
    assert app.summary() == "In Chrome: search"


def test_malformed_actions_json_degrades_to_empty_app_action():
    bad = _rule_to_automation(AutomationRuleEntity(name="broken", actions_json="{ not json"))
    assert bad.kind == AUTOMATION_APP_ACTION
    assert bad.instruction == ""
    assert bad.name == "broken"  # still listable, not dropped


def test_crud_against_the_real_automation_rules_table(tmp_path):
    """The encode/decode contract against the actual SQLite schema + repository
    — insert, list, update, delete all round-trip through real SQL."""
    from winspark.data.connection import ConnectionFactory
    from winspark.data.repositories import AutomationRuleRepository

    factory = ConnectionFactory(tmp_path / "auto.db")
    factory.initialize_schema()
    repo = AutomationRuleRepository(factory)

    new_id = repo.insert(_automation_to_rule(Automation(
        None, "Ping Mum", AUTOMATION_WHATSAPP, "Mum", "Mum", "hello", True)))
    listed = [_rule_to_automation(r) for r in repo.get_all()]
    assert len(listed) == 1 and listed[0].name == "Ping Mum" and listed[0].instruction == "hello"

    repo.update(_automation_to_rule(Automation(
        new_id, "Ping Mum", AUTOMATION_WHATSAPP, "Mum", "Mum", "hi again", True)))
    assert _rule_to_automation(repo.get_by_id(new_id)).instruction == "hi again"

    repo.delete(new_id)
    assert repo.get_all() == []


# --- schedule due-logic ------------------------------------------------------

def test_interval_fires_after_the_interval_not_before():
    a = _sched(mode="interval", minutes=30)
    seeded = datetime(2026, 7, 5, 10, 0)
    assert _schedule_is_due(a, seeded + timedelta(minutes=10), seeded) is False
    assert _schedule_is_due(a, seeded + timedelta(minutes=30), seeded) is True


def test_interval_never_fires_on_first_load():
    # last_fire=None means "just seeded" — must not fire instantly on startup.
    assert _schedule_is_due(_sched(minutes=5), datetime(2026, 7, 5, 10, 0), None) is False


def test_daily_fires_once_when_the_time_passes():
    a = _sched(mode="daily", time="09:00")
    before = datetime(2026, 7, 5, 8, 59)
    after = datetime(2026, 7, 5, 9, 1)
    fired_earlier_today = datetime(2026, 7, 5, 8, 0)
    already_fired = datetime(2026, 7, 5, 9, 0, 30)
    assert _schedule_is_due(a, before, fired_earlier_today) is False
    assert _schedule_is_due(a, after, fired_earlier_today) is True
    assert _schedule_is_due(a, after, already_fired) is False  # not twice in a day


def test_screen_trigger_round_trips_with_watch_fields():
    a = Automation(
        7, "Alert", AUTOMATION_WHATSAPP, "Me", "Me", "error seen!", True,
        trigger_type=TRIGGER_SCREEN, watch_process="chrome.exe",
        watch_display="Chrome", watch_text="error",
    )
    back = _rule_to_automation(_automation_to_rule(a))
    assert back == a
    assert back.trigger_summary() == "When Chrome shows “error”"


def test_semantic_screen_trigger_round_trips_and_reads_by_meaning():
    a = Automation(
        8, "Build watch", AUTOMATION_WHATSAPP, "Me", "Me", "ping!", True,
        trigger_type=TRIGGER_SCREEN, watch_process="code.exe", watch_display="VS Code",
        watch_text="the build failed", watch_mode="meaning",
    )
    back = _rule_to_automation(_automation_to_rule(a))
    assert back.watch_mode == "meaning"
    assert back.trigger_summary() == "When VS Code shows “the build failed” (by meaning)"
