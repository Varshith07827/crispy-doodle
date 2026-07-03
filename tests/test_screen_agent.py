"""Unit tests for the "Do it" agent's plan contract: strict JSON parsing,
key whitelisting, risky detection, and step wording. Execution against real
windows is exercised by a live smoke, not here."""

import json

from winspark.automation.screen_agent import (
    ActionPlan,
    ControlInfo,
    PlanStep,
    build_plan_user_prompt,
    describe_step,
    format_controls_for_ai,
    keys_to_sendkeys,
    parse_plan,
)

CONTROLS = [
    ControlInfo(0, "ButtonControl", "Save"),
    ControlInfo(1, "EditControl", "Search"),
    ControlInfo(2, "ButtonControl", "Send message", automation_id="sendBtn"),
]


def _plan_json(steps, summary="do it"):
    return json.dumps({"summary": summary, "steps": steps})


def test_parse_happy_path_resolves_control_names():
    plan, error = parse_plan(
        _plan_json([
            {"action": "click", "control": 0, "risky": False, "why": "save the file"},
            {"action": "type", "control": 1, "text": "shoes", "risky": False},
            {"action": "press", "keys": "ENTER"},
            {"action": "wait", "seconds": 1},
        ]),
        CONTROLS,
    )
    assert error == ""
    assert [s.action for s in plan.steps] == ["click", "type", "press", "wait"]
    assert plan.steps[0].control_name == "Save"
    assert plan.steps[1].text == "shoes"
    assert plan.steps[2].keys == "ENTER"
    assert plan.has_risky_step is False


def test_parse_accepts_code_fenced_json():
    plan, error = parse_plan("```json\n" + _plan_json([{"action": "press", "keys": "TAB"}]) + "\n```", CONTROLS)
    assert error == ""
    assert plan.steps[0].keys == "TAB"


def test_parse_rejects_unknown_action():
    plan, error = parse_plan(_plan_json([{"action": "format_disk"}]), CONTROLS)
    assert plan is None and error


def test_parse_rejects_out_of_range_control():
    plan, error = parse_plan(_plan_json([{"action": "click", "control": 99}]), CONTROLS)
    assert plan is None and "control" in error.lower()


def test_parse_rejects_disallowed_keys():
    plan, error = parse_plan(_plan_json([{"action": "press", "keys": "WIN+R"}]), CONTROLS)
    assert plan is None and "key" in error.lower()


def test_parse_rejects_oversized_plans():
    steps = [{"action": "press", "keys": "TAB"}] * 11
    plan, error = parse_plan(_plan_json(steps), CONTROLS)
    assert plan is None and "too long" in error.lower()


def test_parse_rejects_non_json():
    plan, error = parse_plan("Sure! First click the Save button...", CONTROLS)
    assert plan is None and error


def test_parse_clamps_wait_seconds():
    plan, _ = parse_plan(_plan_json([{"action": "wait", "seconds": 999}]), CONTROLS)
    assert plan.steps[0].seconds == 5.0


def test_risky_marked_by_ai_flag():
    plan, _ = parse_plan(_plan_json([{"action": "click", "control": 0, "risky": True}]), CONTROLS)
    assert plan.has_risky_step is True


def test_risky_forced_by_control_name_keyword():
    # The AI says risky:false, but the control is literally "Send message" —
    # our keyword net overrides.
    plan, _ = parse_plan(_plan_json([{"action": "click", "control": 2, "risky": False}]), CONTROLS)
    assert plan.has_risky_step is True


def test_risky_forced_by_instruction_keyword():
    plan, _ = parse_plan(
        _plan_json([{"action": "click", "control": 0, "risky": False}]),
        CONTROLS,
        instruction="delete everything",
    )
    assert plan.has_risky_step is True


def test_keys_whitelist():
    assert keys_to_sendkeys("ENTER") == "{Enter}"
    assert keys_to_sendkeys("ctrl+s") == "{Ctrl}s"
    assert keys_to_sendkeys("CTRL+SHIFT+S") == "{Ctrl}{Shift}s"
    assert keys_to_sendkeys("F5") == "{F5}"
    assert keys_to_sendkeys("WIN+R") is None
    assert keys_to_sendkeys("") is None
    assert keys_to_sendkeys("CTRL+ALT+DELETE") == "{Ctrl}{Alt}{Delete}"


def test_describe_step_wording():
    assert describe_step(PlanStep(action="click", control_name="Save")) == "Click “Save”"
    assert describe_step(PlanStep(action="type", control_name="Search", text="shoes")) == "Type “shoes” into “Search”"
    assert describe_step(PlanStep(action="press", keys="ctrl+s")) == "Press CTRL+S"
    assert describe_step(PlanStep(action="wait", seconds=1.5)) == "Wait 1.5s"


def test_prompt_contains_controls_and_instruction():
    prompt = build_plan_user_prompt("Notepad", "type hello", CONTROLS, "some screen text")
    assert "Notepad" in prompt
    assert "type hello" in prompt
    assert '[0] button "Save"' in prompt
    assert "some screen text" in prompt


def test_format_controls_uses_friendly_kinds():
    text = format_controls_for_ai(CONTROLS)
    assert '[1] text field "Search"' in text
