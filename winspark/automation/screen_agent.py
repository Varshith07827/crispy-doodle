"""The "Do it" agent: tell winSpark what to do in any app, in plain English,
and it does it — the acting half of the Comet-style assistant.

Pipeline: enumerate the window's real interactive controls via UI Automation
(the Python take on the .NET WindowIntelligenceEngine) + OCR the screen → hand
both to the AI with the user's instruction → get back a STRICT, validated JSON
plan built from a tiny verb set (click / type / press / wait) → execute on the
STA thread with the same foreground- and viewport-safety the WhatsApp sender
uses.

Safety posture, learned the hard way elsewhere in this codebase:
- The plan is data, not code: unknown verbs, out-of-range control numbers,
  non-whitelisted keys, or oversized plans are rejected wholesale (fail
  closed), never "best-effort executed".
- Every step that could change something is marked risky either by the AI or
  by our own keyword net (belt and braces); the UI pauses for approval in
  "ask before risky" mode.
- At execution time controls are re-located by (type, name) — the elements
  enumerated at plan time are stale after the AI round-trip — and a control
  is only clicked if its click point actually lies inside the window's
  on-screen rectangle (a realized control is NOT necessarily a visible one).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    import win32gui

    _UIA_AVAILABLE = True
except ImportError:  # pragma: no cover - off-Windows
    _UIA_AVAILABLE = False

_MAX_CONTROLS = 80
_MAX_STEPS = 10
_MAX_TYPE_TEXT = 500
_MAX_WAIT_SECONDS = 5.0

# Control types a user-facing action can target.
_ACTIONABLE_TYPES = {
    "ButtonControl",
    "EditControl",
    "ComboBoxControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "HyperlinkControl",
    "ListItemControl",
    "TabItemControl",
    "MenuItemControl",
    "SplitButtonControl",
    "DocumentControl",
}

# Words that make a step risky regardless of what the AI claims — acting on
# other people's apps, the cost of a wrong "send" dwarfs a redundant prompt.
_RISKY_WORDS = {
    "send", "delete", "remove", "pay", "purchase", "buy", "order", "submit",
    "post", "publish", "confirm", "transfer", "share", "forward", "reply",
    "discard", "uninstall", "format", "overwrite", "close",
}

_ALLOWED_MODIFIERS = {"CTRL": "{Ctrl}", "ALT": "{Alt}", "SHIFT": "{Shift}"}
_ALLOWED_BASE_KEYS = {
    "ENTER": "{Enter}", "TAB": "{Tab}", "ESC": "{Esc}", "ESCAPE": "{Esc}",
    "SPACE": " ", "BACKSPACE": "{Back}", "DELETE": "{Delete}",
    "UP": "{Up}", "DOWN": "{Down}", "LEFT": "{Left}", "RIGHT": "{Right}",
    "HOME": "{Home}", "END": "{End}", "PAGEUP": "{PageUp}", "PAGEDOWN": "{PageDown}",
    **{f"F{i}": f"{{F{i}}}" for i in range(1, 13)},
    **{c: c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
}


@dataclass(frozen=True, slots=True)
class ControlInfo:
    """One interactive control as shown to the AI (and used to re-find it)."""

    index: int
    control_type: str
    name: str
    automation_id: str = ""


@dataclass(frozen=True, slots=True)
class PlanStep:
    action: str  # click | type | press | wait
    risky: bool = False
    why: str = ""
    text: str = ""      # for type
    keys: str = ""      # for press (already validated, original form e.g. "CTRL+S")
    seconds: float = 0  # for wait
    # Resolved from the plan-time control list, used to re-find at execution.
    control_index: int = -1
    control_type: str = ""
    control_name: str = ""
    control_automation_id: str = ""


@dataclass(frozen=True, slots=True)
class ActionPlan:
    summary: str
    steps: tuple[PlanStep, ...] = field(default_factory=tuple)

    @property
    def has_risky_step(self) -> bool:
        return any(step.risky for step in self.steps)


# --- perceive: what can be acted on -----------------------------------------

def list_controls_sync(window_handle: int, max_controls: int = _MAX_CONTROLS) -> list[ControlInfo]:
    """Enumerate the window's visible, enabled, actionable controls. STA-thread
    only. Only controls whose click point is inside the window's on-screen
    rectangle are included — same viewport lesson as the chat-row clicking."""
    if not _UIA_AVAILABLE:
        return []
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return []
    try:
        win_left, win_top, win_right, win_bottom = win32gui.GetWindowRect(window_handle)
    except Exception:  # noqa: BLE001
        return []

    controls: list[ControlInfo] = []

    def walk(ctrl, depth: int = 0) -> None:
        if len(controls) >= max_controls or depth > 40:
            return
        try:
            control_type = ctrl.ControlTypeName
        except Exception:  # noqa: BLE001 - stale element
            return
        if control_type in _ACTIONABLE_TYPES:
            try:
                name = (ctrl.Name or "").strip()
                rect = ctrl.BoundingRectangle
                enabled = ctrl.IsEnabled
                automation_id = ctrl.AutomationId or ""
            except Exception:  # noqa: BLE001
                name, rect, enabled, automation_id = "", None, False, ""
            if (
                enabled
                and rect is not None
                and rect.right - rect.left >= 6
                and rect.bottom - rect.top >= 6
                and win_left <= (rect.left + rect.right) // 2 <= win_right
                and win_top <= (rect.top + rect.bottom) // 2 <= win_bottom
                and (name or control_type in ("EditControl", "DocumentControl"))
            ):
                label = name[:60] if name else "(text field)"
                controls.append(ControlInfo(len(controls), control_type, label, automation_id))
        try:
            children = ctrl.GetChildren()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            walk(child, depth + 1)

    walk(root)
    return controls


def format_controls_for_ai(controls: list[ControlInfo]) -> str:
    kind = {
        "ButtonControl": "button", "EditControl": "text field", "ComboBoxControl": "dropdown",
        "CheckBoxControl": "checkbox", "RadioButtonControl": "radio", "HyperlinkControl": "link",
        "ListItemControl": "list item", "TabItemControl": "tab", "MenuItemControl": "menu item",
        "SplitButtonControl": "button", "DocumentControl": "text area",
    }
    return "\n".join(f'[{c.index}] {kind.get(c.control_type, c.control_type)} "{c.name}"' for c in controls)


# --- reason: the plan prompt + strict parsing --------------------------------

PLAN_SYSTEM_PROMPT = (
    "You operate a Windows application for the user. You are given the app's "
    "interactive controls (numbered) and the text visible on its screen. "
    "Produce the SHORTEST sequence of steps that fulfils the user's request.\n"
    "Reply with ONLY JSON, no prose, in exactly this shape:\n"
    '{"summary": "what the plan does, one sentence",\n'
    ' "steps": [\n'
    '   {"action": "click", "control": 3, "risky": false, "why": "short reason"},\n'
    '   {"action": "type", "control": 5, "text": "hello", "risky": false, "why": "..."},\n'
    '   {"action": "press", "keys": "CTRL+S", "risky": false, "why": "..."},\n'
    '   {"action": "wait", "seconds": 1}\n'
    " ]}\n"
    "Rules: use ONLY the listed control numbers; allowed keys are ENTER, TAB, ESC, "
    "arrow keys, HOME/END, PAGEUP/PAGEDOWN, F1-F12, letters/digits, optionally with "
    "CTRL/ALT/SHIFT (e.g. CTRL+S). Mark risky:true on any step that sends, posts, "
    "replies, deletes, pays, submits, or closes unsaved work. At most 10 steps. "
    'If the request cannot be done with these controls, return {"summary": "why not", "steps": []}.'
)


def build_plan_user_prompt(app_name: str, instruction: str, controls: list[ControlInfo], screen_text: str) -> str:
    return (
        f"App: {app_name}\n"
        f"User request: {instruction.strip()}\n\n"
        f"Controls:\n{format_controls_for_ai(controls)}\n\n"
        f"Screen text (OCR, may contain errors):\n{screen_text[:3000]}"
    )


def keys_to_sendkeys(keys: str) -> Optional[str]:
    """Translate "CTRL+S" style key specs into uiautomation SendKeys syntax.
    Returns None for anything outside the whitelist — fail closed."""
    parts = [p.strip().upper() for p in keys.split("+") if p.strip()]
    if not parts:
        return None
    *modifiers, base = parts
    if base not in _ALLOWED_BASE_KEYS:
        return None
    prefix = ""
    for modifier in modifiers:
        mapped = _ALLOWED_MODIFIERS.get(modifier)
        if mapped is None:
            return None
        prefix += mapped
    return prefix + _ALLOWED_BASE_KEYS[base]


def _looks_risky(*texts: str) -> bool:
    words = set(re.findall(r"[a-z]+", " ".join(texts).lower()))
    return bool(words & _RISKY_WORDS)


def _strip_fences(reply_text: str) -> str:
    text = reply_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    return text


def _parse_one_step(raw, controls: list[ControlInfo], instruction_risky: bool) -> tuple[Optional[PlanStep], str]:
    """Validate one raw step dict into a PlanStep — shared by the whole-plan
    parser and the one-step-at-a-time loop. Fail closed."""
    if not isinstance(raw, dict):
        return None, "The AI didn't return a usable plan — try rephrasing the request."
    action = str(raw.get("action") or "").strip().lower()
    why = str(raw.get("why") or "").strip()[:120]
    risky = bool(raw.get("risky")) or instruction_risky or _looks_risky(why)

    if action in ("click", "type"):
        control_number = raw.get("control")
        if not isinstance(control_number, int) or not (0 <= control_number < len(controls)):
            return None, "The plan referred to a control that doesn't exist — try again."
        control = controls[control_number]
        risky = risky or _looks_risky(control.name)
        text_value = ""
        if action == "type":
            text_value = str(raw.get("text") or "")
            if not text_value or len(text_value) > _MAX_TYPE_TEXT:
                return None, "The plan tried to type something invalid — try again."
        return PlanStep(
            action=action, risky=risky, why=why, text=text_value,
            control_index=control.index, control_type=control.control_type,
            control_name=control.name, control_automation_id=control.automation_id,
        ), ""
    if action == "press":
        keys = str(raw.get("keys") or "").strip()
        if keys_to_sendkeys(keys) is None:
            return None, f"The plan used a key combination that isn't allowed ({keys or 'empty'})."
        return PlanStep(action="press", risky=risky, why=why, keys=keys), ""
    if action == "wait":
        try:
            seconds = float(raw.get("seconds") or 0)
        except (TypeError, ValueError):
            return None, "The AI didn't return a usable plan — try rephrasing the request."
        return PlanStep(action="wait", seconds=max(0.0, min(seconds, _MAX_WAIT_SECONDS))), ""
    return None, "The AI suggested something winSpark can't do yet."


def parse_plan(reply_text: str, controls: list[ControlInfo], instruction: str = "") -> tuple[Optional[ActionPlan], str]:
    """Validate the AI's reply into an ActionPlan. Returns (plan, "") or
    (None, plain-English error). Anything out of contract rejects the whole
    plan — a half-valid plan must not half-execute."""
    try:
        data = json.loads(_strip_fences(reply_text))
    except (ValueError, TypeError):
        return None, "The AI didn't return a usable plan — try rephrasing the request."

    summary = str(data.get("summary") or "").strip()
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        return None, "The AI didn't return a usable plan — try rephrasing the request."
    if len(raw_steps) > _MAX_STEPS:
        return None, f"The plan was too long ({len(raw_steps)} steps) — try a smaller request."

    instruction_risky = _looks_risky(instruction)
    steps: list[PlanStep] = []
    for raw in raw_steps:
        step, error = _parse_one_step(raw, controls, instruction_risky)
        if step is None:
            return None, error
        steps.append(step)

    return ActionPlan(summary=summary or "Do what you asked.", steps=tuple(steps)), ""


@dataclass(frozen=True, slots=True)
class StepDecision:
    """One turn of the closed-loop agent: either the goal is done (summary
    says what happened), or here's the single next step to take."""

    done: bool
    summary: str = ""
    step: Optional[PlanStep] = None
    # Hash of the screen text the decision was based on — the loop uses it to
    # detect "same step proposed again while nothing on screen changed".
    screen_digest: str = ""


STEP_SYSTEM_PROMPT = (
    "You operate a Windows application for the user, ONE step at a time. Each "
    "turn you see the goal, what you already did (with each step's result), and "
    "the app's CURRENT controls and screen text. The screen reflects what your "
    "previous step actually did — base the next step on it, never on an "
    "assumption about what should have happened.\n"
    "Reply with ONLY JSON — exactly one of:\n"
    '{"done": true, "summary": "what was accomplished, or why it can\'t be"}\n'
    '{"action": "click", "control": 3, "risky": false, "why": "short reason"}\n'
    '{"action": "type", "control": 5, "text": "hello", "risky": false, "why": "..."}\n'
    '{"action": "press", "keys": "CTRL+S", "risky": false, "why": "..."}\n'
    '{"action": "wait", "seconds": 1}\n'
    "Rules: use ONLY the listed control numbers; allowed keys are ENTER, TAB, ESC, "
    "arrow keys, HOME/END, PAGEUP/PAGEDOWN, F1-F12, letters/digits, optionally with "
    "CTRL/ALT/SHIFT. Mark risky:true on any step that sends, posts, replies, deletes, "
    "pays, submits, or closes unsaved work. Do not repeat a step that already "
    "succeeded. When the goal is complete (or impossible with these controls), "
    "return done."
)


def build_step_user_prompt(app_name: str, goal: str, controls: list[ControlInfo], screen_text: str, history: list[str]) -> str:
    done_so_far = "\n".join(f"- {entry}" for entry in history[-8:]) or "- nothing yet"
    return (
        f"App: {app_name}\n"
        f"Goal: {goal.strip()}\n\n"
        f"Already done:\n{done_so_far}\n\n"
        f"Current controls:\n{format_controls_for_ai(controls)}\n\n"
        f"Current screen text (OCR, may contain errors):\n{screen_text[:3000]}"
    )


def parse_step(reply_text: str, controls: list[ControlInfo], goal: str = "") -> tuple[Optional[StepDecision], str]:
    """Validate one loop turn: {"done": ...} or a single action object."""
    try:
        data = json.loads(_strip_fences(reply_text))
    except (ValueError, TypeError):
        return None, "The AI didn't return a usable step — try rephrasing the request."
    if not isinstance(data, dict):
        return None, "The AI didn't return a usable step — try rephrasing the request."

    if data.get("done"):
        summary = str(data.get("summary") or "").strip()
        return StepDecision(done=True, summary=summary or "Done."), ""

    step, error = _parse_one_step(data, controls, _looks_risky(goal))
    if step is None:
        return None, error
    return StepDecision(done=False, step=step), ""


def describe_step(step: PlanStep) -> str:
    if step.action == "click":
        return f"Click “{step.control_name}”"
    if step.action == "type":
        return f"Type “{step.text}” into “{step.control_name}”"
    if step.action == "press":
        return f"Press {step.keys.upper()}"
    if step.action == "wait":
        return f"Wait {step.seconds:g}s"
    return step.action


# --- act: execute on the STA thread ------------------------------------------

def execute_plan_sync(window_handle: int, steps: list[PlanStep]) -> list[tuple[bool, str]]:
    """Run the steps against the window. STA-thread only. Returns one
    (ok, plain-English message) per step; stops at the first failure so a
    broken assumption can't cascade into further wrong actions."""
    if not _UIA_AVAILABLE:
        return [(False, "Doing things in apps is only available on Windows.")]
    from winspark.connectors.whatsapp_group_sender import _ensure_foreground, _send_unicode_text

    if not _ensure_foreground(window_handle):
        return [(False, "The app couldn't be brought to the front — nothing was done.")]

    results: list[tuple[bool, str]] = []
    for step in steps:
        try:
            ok, message = _execute_one(window_handle, step, _send_unicode_text)
        except Exception as ex:  # noqa: BLE001
            ok, message = False, f"{describe_step(step)} failed — {ex}"
        results.append((ok, message))
        if not ok:
            break
        time.sleep(0.25)  # let the app react before the next step
    return results


def _execute_one(window_handle: int, step: PlanStep, send_text) -> tuple[bool, str]:
    if step.action == "wait":
        time.sleep(step.seconds)
        return True, describe_step(step)

    if step.action == "press":
        sendkeys = keys_to_sendkeys(step.keys)
        if sendkeys is None:  # pragma: no cover - parse already rejected it
            return False, f"Key combination {step.keys!r} isn't allowed."
        auto.SendKeys(sendkeys, waitTime=0.15)
        return True, describe_step(step)

    # click / type need the control — re-find it fresh (plan-time elements are
    # stale after the AI round-trip and any earlier steps).
    control = _find_control(window_handle, step)
    if control is None:
        return False, f"Couldn't find “{step.control_name}” anymore — the app may have changed."
    try:
        rect = control.BoundingRectangle
        win_left, win_top, win_right, win_bottom = win32gui.GetWindowRect(window_handle)
        center_x, center_y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        if not (win_left <= center_x <= win_right and win_top <= center_y <= win_bottom):
            return False, f"“{step.control_name}” isn't visible on screen right now."
    except Exception:  # noqa: BLE001
        return False, f"Couldn't find “{step.control_name}” anymore — the app may have changed."

    control.Click(simulateMove=False)
    if step.action == "type":
        time.sleep(0.15)
        send_text(step.text)
    return True, describe_step(step)


def _find_control(window_handle: int, step: PlanStep):
    """Re-locate the step's control by (type, name); automation id breaks ties.
    Ambiguity fails the step — clicking "one of the three Save buttons" is how
    wrong things happen."""
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None

    matches: list = []

    def walk(ctrl, depth: int = 0) -> None:
        if len(matches) > 4 or depth > 40:
            return
        try:
            if ctrl.ControlTypeName == step.control_type:
                name = (ctrl.Name or "").strip()[:60]
                if name == step.control_name or (not name and step.control_name == "(text field)"):
                    matches.append(ctrl)
        except Exception:  # noqa: BLE001
            return
        try:
            children = ctrl.GetChildren()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            walk(child, depth + 1)

    walk(root)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and step.control_automation_id:
        by_id = [m for m in matches if (m.AutomationId or "") == step.control_automation_id]
        if len(by_id) == 1:
            return by_id[0]
    # WhatsApp-style duplicated trees repeat every element twice; two matches
    # with identical name/type/rect are the same thing rendered twice.
    if len(matches) == 2:
        try:
            r0, r1 = matches[0].BoundingRectangle, matches[1].BoundingRectangle
            if (r0.left, r0.top, r0.right, r0.bottom) == (r1.left, r1.top, r1.right, r1.bottom):
                return matches[0]
        except Exception:  # noqa: BLE001
            pass
    return None
