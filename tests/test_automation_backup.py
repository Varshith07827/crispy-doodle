"""Tests for the automations safety net: every change snapshots the WhatsApp
bindings + saved automations to a JSON file next to the database, and startup
restores them when the tables are empty but the backup isn't — so "programmed
previously but gone after a restart" can't happen again, whatever wiped the
table. Deletes re-snapshot the post-delete state, so removed automations stay
removed."""

import json

import pytest

pytest.importorskip("PySide6")

from winspark.data.connection import ConnectionFactory
from winspark.ui.engine_host import AUTOMATION_WHATSAPP, TRIGGER_SCHEDULE, EngineHost


@pytest.fixture
def factory(tmp_path):
    f = ConnectionFactory(tmp_path / "w.db")
    f.initialize_schema()
    return f


def _host(factory, start=False):
    host = EngineHost(factory)
    if start:
        host.start()  # binding writes go through the engine loop
    return host


def test_every_change_writes_a_backup_next_to_the_db(factory, tmp_path):
    host = _host(factory, start=True)
    host.add_or_update_binding("Family", "", 3, enabled=False, reply_source="trigger",
                               trigger_text="ping", reply_text="pong")
    host.save_automation(None, "Morning ping", AUTOMATION_WHATSAPP, "Family", "Family", "Good morning!",
                         trigger_type=TRIGGER_SCHEDULE, schedule_mode="daily", daily_time="09:00")
    host.shutdown()

    backup = tmp_path / "automations-backup.json"
    assert backup.exists()
    data = json.loads(backup.read_text(encoding="utf-8"))
    assert [b["group_name"] for b in data["bindings"]] == ["Family"]
    assert [r["name"] for r in data["rules"]] == ["Morning ping"]
    assert data["rules"][0]["trigger_type"] == TRIGGER_SCHEDULE


def test_wiped_tables_restore_from_backup(factory):
    host = _host(factory, start=True)
    host.add_or_update_binding("Family", "", 3, enabled=False, reply_source="trigger",
                               trigger_text="ping", reply_text="pong")
    host.save_automation(None, "Morning ping", AUTOMATION_WHATSAPP, "Family", "Family", "Good morning!")
    host.shutdown()

    # Simulate the reported loss: tables emptied outside the app's own flows.
    conn = factory.create_connection()
    conn.execute("DELETE FROM WhatsAppFetchBindings")
    conn.execute("DELETE FROM AutomationRules")
    conn.close()

    fresh = _host(factory)
    assert fresh.get_bindings() == [] and fresh.get_automations() == []
    fresh._maybe_restore_automations()  # what start() runs before engines resume

    bindings = fresh.get_bindings()
    assert [b.group_name for b in bindings] == ["Family"]
    assert bindings[0].trigger_text == "ping" and bindings[0].reply_text == "pong"
    autos = fresh.get_automations()
    assert [a.name for a in autos] == ["Morning ping"]
    fresh.shutdown()


def test_deleted_automations_stay_deleted(factory):
    host = _host(factory, start=True)
    host.add_or_update_binding("Family", "", 3, enabled=False, reply_source="trigger",
                               trigger_text="ping", reply_text="pong")
    binding = host.get_bindings()[0]
    host.delete_binding(binding.binding_id)  # user really removed it
    host.shutdown()

    fresh = _host(factory)
    fresh._maybe_restore_automations()
    assert fresh.get_bindings() == []  # not resurrected
    fresh.shutdown()


def test_restore_never_overwrites_existing_data(factory):
    host = _host(factory, start=True)
    host.add_or_update_binding("Family", "", 3, enabled=False, reply_source="trigger",
                               trigger_text="ping", reply_text="pong")
    host.add_or_update_binding("Work", "", 3, enabled=False, reply_source="trigger",
                               trigger_text="hi", reply_text="hello")
    # Table is non-empty — restore must be a no-op even with a backup present.
    host._maybe_restore_automations()
    assert sorted(b.group_name for b in host.get_bindings()) == ["Family", "Work"]
    host.shutdown()


def test_broken_backup_never_blocks_startup(factory, tmp_path):
    (tmp_path / "automations-backup.json").write_text("{ not json", encoding="utf-8")
    host = _host(factory)
    host._maybe_restore_automations()  # must not raise
    assert host.get_bindings() == []
    host.shutdown()


def test_reply_config_routes_to_the_web_search_model(factory):
    host = _host(factory)
    host.set_openai_config("sk-x", "gpt-4o", "openai")
    host.set_ai_web_search(True)
    api_key, model, base_url, fallback = host._read_reply_config()
    assert model == "gpt-4o-mini-search-preview" and fallback == "gpt-4o"

    host.set_ai_web_search(False)
    api_key, model, base_url, fallback = host._read_reply_config()
    assert model == "gpt-4o" and fallback == ""

    # the acting agent always stays on the configured model
    assert host._read_openai_config()[1] == "gpt-4o"
    host.shutdown()


def test_posting_to_the_inbox_triggers_an_immediate_poll(factory):
    import time

    host = _host(factory, start=True)
    polled = []

    async def rec(binding_id):
        polled.append(binding_id)

    host._relay_service.poll_binding_now_async = rec
    host._relay_service._relay_enabled = True  # act as if the relay is on
    host.add_or_update_binding("LiveChat", "", 300, enabled=True, reply_source="web")
    host.add_or_update_binding("Muted", "", 300, enabled=False, reply_source="web")
    live_id = next(b.binding_id for b in host.get_bindings() if b.group_name == "LiveChat")

    host._on_inbox_message("LiveChat")   # what the mock server fires on a POST
    host._on_inbox_message("Muted")      # disabled -> ignored
    host._on_inbox_message("Nobody")     # no binding -> ignored

    deadline = time.monotonic() + 3
    while live_id not in polled and time.monotonic() < deadline:
        time.sleep(0.02)
    assert polled == [live_id]           # only the enabled, matching chat
    host.shutdown()


def test_inbox_message_is_ignored_when_automation_is_off(factory):
    host = _host(factory, start=True)
    polled, sent = [], []

    async def rec(binding_id):
        polled.append(binding_id)

    async def send(chat, text):
        sent.append((chat, text)); return (True, "ok")

    host._relay_service.poll_binding_now_async = rec
    host._send_whatsapp_for_watcher = send
    host._relay_service._relay_enabled = False   # master switch OFF
    host.add_or_update_binding("LiveChat", "", 300, enabled=True, reply_source="web")
    host._mock_server.inject_message("LiveChat", "hi")
    host._on_inbox_message("LiveChat")
    host._on_inbox_message("NoBinding")
    import time
    time.sleep(0.3)
    assert polled == []                          # nothing fires while off
    host.shutdown()


def test_posting_to_a_chat_with_no_automation_auto_binds_and_polls(factory):
    import time

    host = _host(factory, start=True)
    polled = []

    async def rec(binding_id):
        polled.append(binding_id)

    host._relay_service.poll_binding_now_async = rec
    host._relay_service._relay_enabled = True
    assert host.get_bindings() == []
    host._mock_server.inject_message("Karthik", "rey pattinchukoku")
    host._on_inbox_message("Karthik")            # POST to an unbound chat

    # a web automation was created for the chat, on the spot...
    bindings = host.get_bindings()
    assert [ (b.group_name, b.reply_source, b.is_enabled) for b in bindings ] == [("Karthik", "web", True)]
    # ...and its binding was polled to send the queued message (serially)
    deadline = time.monotonic() + 3
    while not polled and time.monotonic() < deadline:
        time.sleep(0.02)
    assert polled and set(polled) == {bindings[0].binding_id}   # its binding, polled to flush the queue
    host.shutdown()


def test_a_burst_of_posts_creates_only_one_binding(factory):
    import time

    host = _host(factory, start=True)

    async def rec(binding_id):
        pass

    host._relay_service.poll_binding_now_async = rec
    host._relay_service._relay_enabled = True
    for i in range(10):                          # the reported burst
        host._mock_server.inject_message("Manohar", f"cmd #{i}")
        host._on_inbox_message("Manohar")
    time.sleep(0.2)
    assert [b.group_name for b in host.get_bindings()] == ["Manohar"]   # exactly one
    host.shutdown()


def test_webhook_testing_off_ignores_posts(factory):
    import time

    host = _host(factory, start=True)
    polled = []

    async def rec(binding_id):
        polled.append(binding_id)

    host._relay_service.poll_binding_now_async = rec
    host._relay_service._relay_enabled = True
    host.set_webhook_testing_enabled(False)
    host._mock_server.inject_message("Karthik", "hi")
    host._on_inbox_message("Karthik")
    time.sleep(0.2)
    assert host.get_bindings() == [] and polled == []   # nothing happens
    host.shutdown()


def test_a_chat_can_run_one_automation_per_type_side_by_side(factory):
    host = _host(factory, start=True)

    host.add_or_update_binding("Manohar", "", 3, enabled=True, reply_source="web")
    host.add_or_update_binding("Manohar", "", 3, enabled=True, reply_source="openai",
                               ai_mode="reply", ai_prompt="Be brief.")
    host.add_or_update_binding("Manohar", "", 3, enabled=True, reply_source="trigger",
                               trigger_text="ping", reply_text="pong")

    assert sorted(b.reply_source for b in host.get_chat_bindings("Manohar")) == ["openai", "trigger", "web"]

    # Re-saving a type updates it in place — no fourth automation appears.
    host.add_or_update_binding("Manohar", "", 3, enabled=True, reply_source="openai",
                               ai_mode="reply", ai_prompt="Be VERY brief.")
    bindings = host.get_chat_bindings("Manohar")
    assert len(bindings) == 3
    assert next(b for b in bindings if b.reply_source == "openai").ai_prompt == "Be VERY brief."
    host.shutdown()


def test_stopping_one_type_leaves_the_others_running(factory):
    host = _host(factory, start=True)
    host.set_relay_enabled(True)
    host.add_or_update_binding("Manohar", "", 3, enabled=True, reply_source="web")
    host.add_or_update_binding("Manohar", "", 3, enabled=True, reply_source="openai")

    host.stop_chat_automation("Manohar", "openai")

    assert host.is_chat_automation_running("Manohar", "web")
    assert not host.is_chat_automation_running("Manohar", "openai")
    assert host.is_chat_automation_running("Manohar")   # any type still counts

    host.stop_chat_automation("Manohar")                # no type = stop them all
    assert not host.is_chat_automation_running("Manohar")
    host.shutdown()


def test_inbox_post_targets_the_chats_web_automation_not_the_ai_one(factory):
    import time

    host = _host(factory, start=True)
    polled = []

    async def rec(binding_id):
        polled.append(binding_id)

    host._relay_service.poll_binding_now_async = rec
    host._relay_service._relay_enabled = True
    # The chat already runs an AI automation; a POST must not reuse it.
    host.add_or_update_binding("Manohar", "", 3, enabled=True, reply_source="openai")
    host._mock_server.inject_message("Manohar", "hello")
    host._on_inbox_message("Manohar")

    deadline = time.monotonic() + 3
    while not polled and time.monotonic() < deadline:
        time.sleep(0.02)
    bindings = {b.reply_source: b for b in host.get_chat_bindings("Manohar")}
    assert set(bindings) == {"openai", "web"}           # a web automation was added
    assert polled and polled[0] == bindings["web"].binding_id
    host.shutdown()


def test_manual_run_routes_agent_questions_to_ask_user(factory):
    """An attended run (Run now in the Automations panel) answers the agent's
    question inline; the answer lands in the history the agent sees next."""
    from winspark.automation.screen_agent import StepDecision

    host = _host(factory)
    seen_histories = []

    def fake_next_step(handle, app, goal, history):
        seen_histories.append(list(history))
        if len(seen_histories) == 1:
            return True, StepDecision(done=False, question="What is today's date?")
        return True, StepDecision(done=True, summary="Booked.")

    host.agent_next_step = fake_next_step

    ok, summary = host._run_agent_goal(1, "Chrome", "book for tomorrow",
                                       ask_user=lambda q: "12 July 2026")

    assert ok and summary == "Booked."
    assert seen_histories[1] == ["Asked you: What is today's date? -> you said: 12 July 2026"]
    host.shutdown()


def test_unattended_run_still_stops_at_a_question(factory):
    from winspark.automation.screen_agent import StepDecision

    host = _host(factory)
    host.agent_next_step = lambda *a: (True, StepDecision(done=False, question="Which one?"))

    ok, summary = host._run_agent_goal(1, "Chrome", "do the thing")   # no ask_user

    assert not ok and "Which one?" in summary
    host.shutdown()
