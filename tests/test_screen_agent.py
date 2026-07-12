"""Unit tests for the "Do it" agent's plan contract: strict JSON parsing,
key whitelisting, risky detection, and step wording. Execution against real
windows is exercised by a live smoke, not here."""

import json

from winspark.automation.screen_agent import (
    ActionPlan,
    ControlInfo,
    PlanStep,
    build_plan_user_prompt,
    build_step_user_prompt,
    describe_step,
    format_controls_for_ai,
    keys_to_sendkeys,
    parse_plan,
    parse_step,
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


# --- the closed loop's one-step contract -------------------------------------

def test_parse_step_done_form():
    decision, error = parse_step('{"done": true, "summary": "Saved the file."}', CONTROLS)
    assert error == ""
    assert decision.done is True
    assert decision.summary == "Saved the file."
    assert decision.step is None


def test_parse_step_action_form_resolves_control():
    decision, error = parse_step('{"action": "click", "control": 0, "risky": false, "why": "save"}', CONTROLS)
    assert error == ""
    assert decision.done is False
    assert decision.step.control_name == "Save"


def test_parse_step_applies_the_same_validation_as_plans():
    decision, error = parse_step('{"action": "press", "keys": "WIN+R"}', CONTROLS)
    assert decision is None and "key" in error.lower()

    decision, error = parse_step('{"action": "click", "control": 99}', CONTROLS)
    assert decision is None and error

    decision, error = parse_step("First, I would click...", CONTROLS)
    assert decision is None and error


def test_parse_step_risky_forced_by_goal_keyword():
    decision, _ = parse_step('{"action": "click", "control": 0, "risky": false}', CONTROLS, goal="delete the file")
    assert decision.step.risky is True


def test_step_prompt_carries_history_and_current_screen():
    prompt = build_step_user_prompt(
        "Notepad", "save it", CONTROLS, "current screen words",
        ["Click “Save” -> ok", "Press CTRL+S -> FAILED: dialog appeared"],
    )
    assert "Click “Save” -> ok" in prompt
    assert "FAILED: dialog appeared" in prompt
    assert "current screen words" in prompt
    assert '[0] button "Save"' in prompt


def test_step_prompt_says_nothing_yet_on_first_round():
    prompt = build_step_user_prompt("Notepad", "save it", CONTROLS, "", [])
    assert "nothing yet" in prompt


def test_parse_step_ask_form_returns_the_question():
    decision, error = parse_step('{"ask": "Which account should I use?"}', CONTROLS)
    assert error == ""
    assert decision.done is False and decision.step is None
    assert decision.question == "Which account should I use?"


def test_step_prompt_includes_what_worked_before():
    prompt = build_step_user_prompt(
        "Notepad", "save it", CONTROLS, "words", [],
        learned=["Goal “save it”: Click “Save”; Press CTRL+S"],
    )
    assert "What worked in this app before" in prompt
    assert "Press CTRL+S" in prompt


def test_step_prompt_history_window_is_generous():
    history = [f"step {i} -> ok" for i in range(30)]
    prompt = build_step_user_prompt("Notepad", "goal", CONTROLS, "", history)
    assert "step 29 -> ok" in prompt and "step 10 -> ok" in prompt   # last 20 kept
    assert "step 5 -> ok" not in prompt


def test_browser_tab_reader_excludes_in_page_tabs():
    # Fake UIA tree: browser chrome has 2 TabItems; the page's DocumentControl
    # holds an in-page tab strip (YouTube-style chips) that must NOT count.
    import winspark.automation.screen_agent as sa

    class Node:
        def __init__(self, ct, name="", children=(), selected=False):
            self.ControlTypeName = ct
            self.Name = name
            self._children = list(children)
            self._selected = selected
        def GetChildren(self):
            return list(self._children)
        def GetSelectionItemPattern(self):
            node = self
            class P:
                IsSelected = node._selected
            return P()

    tree = Node("PaneControl", children=[
        Node("TabControl", children=[
            Node("TabItemControl", "Gmail"),
            Node("TabItemControl", "LeetCode", selected=True),
        ]),
        Node("DocumentControl", children=[   # the web page — pruned
            Node("TabControl", children=[
                Node("TabItemControl", "All"),
                Node("TabItemControl", "Music"),
            ]),
        ]),
    ])
    orig_avail, orig_from = sa._UIA_AVAILABLE, sa.auto.ControlFromHandle
    sa._UIA_AVAILABLE = True
    sa.auto.ControlFromHandle = lambda _h: tree
    try:
        tabs = sa.list_browser_tabs_sync(123)
    finally:
        sa._UIA_AVAILABLE = orig_avail
        sa.auto.ControlFromHandle = orig_from

    assert [name for name, _ in tabs] == ["Gmail", "LeetCode"]   # no All/Music
    assert dict(tabs)["LeetCode"] is True                        # current tab marked


def test_step_prompt_tells_the_agent_todays_date():
    """"Book a flight for tomorrow" needs the date — without it the agent had
    to stop and ask the user "What is today's date?" (seen live)."""
    from datetime import datetime

    prompt = build_step_user_prompt("Chrome", "book a flight for tomorrow", CONTROLS, "", [])
    assert f"Today is {datetime.now().strftime('%A, %d %B %Y')}." in prompt


def test_user_answers_never_scroll_out_of_the_prompt():
    """Answers were trimmed away with old steps (history[-20:]) — after 20 more
    steps the agent forgot what it was told and re-asked. Now what the user
    said sits in its own section, kept in full."""
    history = ["Asked you: Which account? -> you said: the work one"]
    history += [f"Click “Next” -> ok" for _ in range(30)]     # push it far past the window
    history += ["User guidance: pick the morning flight"]

    prompt = build_step_user_prompt("Chrome", "book for tomorrow", CONTROLS, "", history)

    assert "do NOT ask these again" in prompt
    assert "you said: the work one" in prompt                     # survived 30 newer steps
    assert "User guidance: pick the morning flight" in prompt
    # ...and answers are not duplicated into the trimmed step list.
    assert prompt.count("the work one") == 1
