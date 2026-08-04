"""Tests for the automations safety net: every change snapshots the WhatsApp
bindings + saved automations to a JSON file next to the database, and startup
restores them when the tables are empty but the backup isn't — so "programmed
previously but gone after a restart" can't happen again, whatever wiped the
table. Deletes re-snapshot the post-delete state, so removed automations stay
removed."""

import asyncio
import json
import threading

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


def test_a_repeated_question_reuses_the_answer_instead_of_renagging(factory):
    """Seen live: the agent asked the same question again right after it was
    answered. The loop now replays the stored answer (asking the user only
    once) — and if the agent STILL insists, the run stops instead of looping."""
    from winspark.automation.screen_agent import StepDecision

    host = _host(factory)
    asks = []

    def fake_next_step(handle, app, goal, history):
        if len(asks) < 3:   # the model stubbornly re-asks forever
            return True, StepDecision(done=False, question="What is today's date?")
        return True, StepDecision(done=True, summary="Booked.")

    host.agent_next_step = fake_next_step

    def answer(question):
        asks.append(question)
        return "12 July 2026"

    ok, summary = host._run_agent_goal(1, "Chrome", "book for tomorrow", ask_user=answer)

    assert asks == ["What is today's date?"]     # the user was asked exactly ONCE
    assert not ok and "kept re-asking" in summary
    host.shutdown()


class _Msg:
    def __init__(self, sender, text, is_incoming):
        self.sender, self.text, self.is_incoming = sender, text, is_incoming
        # Match the real WhatsAppMessage's attachment/timestamp fields.
        self.media_kind, self.media_note, self.time_text, self.media_rect = "", "", "", None


def _remember_and_wait(host, chat, msgs):
    """_remember_conversation now writes on a background thread; join it so the
    assertion sees the completed write (production fires and forgets)."""
    thread = host._remember_conversation(0, chat, msgs)
    if thread is not None:
        thread.join(timeout=5)


# --- background capture -------------------------------------------------------
#
# Chat memory used to be recorded ONLY by the WhatsApp panel's 3-second timer,
# which starts in showEvent and stops in hideEvent. Opening Settings or
# Automations silently stopped recording the conversation — so messages reached
# MongoDB only while the user happened to be looking at that chat.

class _FakeConnectorFor:
    """Stands in for the WhatsApp connector: reports one open conversation."""

    def __init__(self, active, messages):
        self.active, self.messages, self.reads = active, messages, 0

    async def find_window_async(self):
        return 4242

    async def read_open_conversation_async(self, handle, limit=20):
        self.reads += 1
        return self.active, list(self.messages)


def _host_with_open_chat(factory, active, messages):
    """A host whose WhatsApp reads are faked and run inline — the real _submit
    hands work to the engine loop, which these tests don't start."""
    host = _host(factory)
    host._connector = _FakeConnectorFor(active, messages)
    host._submit = lambda coro, timeout=None: asyncio.run(coro)
    return host, host._connector


def _capture_and_wait(host):
    """Capture writes memory on a background thread; join it so assertions see
    the finished write (production fires and forgets)."""
    before = set(threading.enumerate())
    host._capture_open_conversation()
    for thread in set(threading.enumerate()) - before:
        thread.join(timeout=5)


def test_capture_records_the_open_chat_without_any_panel_open(factory):
    """The fix: capture no longer depends on which panel is on screen."""
    host, connector = _host_with_open_chat(
        factory, "Varshith", [_Msg("Varshith", "you around?", True)])

    _capture_and_wait(host)

    assert connector.reads == 1
    assert host.get_chat_memory("Varshith") == [("them", "Varshith", "you around?")]
    host.shutdown()


def test_repeated_capture_of_an_unchanged_chat_writes_nothing_new(factory):
    """It runs every 3 seconds — an idle chat must not re-write on every tick."""
    host, connector = _host_with_open_chat(
        factory, "Varshith", [_Msg("Varshith", "you around?", True)])

    for _ in range(5):
        _capture_and_wait(host)

    assert connector.reads == 5                                   # read each tick
    assert len(host.get_chat_memory("Varshith")) == 1             # written once
    host.shutdown()


def test_capture_does_nothing_when_no_conversation_is_open(factory):
    host, connector = _host_with_open_chat(factory, None, [])
    host._capture_open_conversation()

    assert host.get_chats_with_memory() == []
    host.shutdown()


def test_capture_is_skipped_while_automations_are_paused(factory):
    """The master pause switch means 'winSpark does nothing on its own'. A
    background read of the user's chats has to honour that."""
    host, connector = _host_with_open_chat(factory, "Varshith", [_Msg("V", "hi", True)])
    host.set_automations_paused(True)

    host._capture_open_conversation()

    assert connector.reads == 0          # never even looked
    assert host.get_chats_with_memory() == []
    host.shutdown()


def test_capture_without_a_connector_is_a_no_op(factory):
    """Off-Windows, or before WhatsApp is available, the loop must not raise."""
    host = _host(factory)
    host._connector = None
    host._capture_open_conversation()     # must not raise
    host.shutdown()


def test_viewing_a_conversation_stores_it_as_that_chats_memory(factory):
    host = _host(factory)
    msgs = [
        _Msg("Karthik", "you free this evening?", True),
        _Msg("", "yeah what's up", False),
    ]
    _remember_and_wait(host, "Karthik", msgs)

    assert host.get_chat_memory("Karthik") == [
        ("them", "Karthik", "you free this evening?"),
        ("me", "", "yeah what's up"),
    ]
    host.shutdown()


def test_each_chat_keeps_its_own_separate_memory(factory):
    host = _host(factory)
    _remember_and_wait(host, "Karthik", [_Msg("Karthik", "hi from Karthik", True)])
    _remember_and_wait(host, "Manohar", [_Msg("Manohar", "hi from Manohar", True)])

    assert host.get_chat_memory("Karthik") == [("them", "Karthik", "hi from Karthik")]
    assert host.get_chat_memory("Manohar") == [("them", "Manohar", "hi from Manohar")]
    host.shutdown()


def test_unchanged_conversation_is_not_rewritten_each_poll(factory):
    host = _host(factory)
    writes = []
    orig = host._chat_memory.append_chat_memory
    host._chat_memory.append_chat_memory = lambda *a, **k: (writes.append(a) or orig(*a, **k))

    convo = [_Msg("Karthik", "same message", True)]
    _remember_and_wait(host, "Karthik", convo)   # first: writes
    n_after_first = len(writes)
    _remember_and_wait(host, "Karthik", convo)   # identical: must NOT rewrite
    assert len(writes) == n_after_first

    _remember_and_wait(host, "Karthik", convo + [_Msg("", "new reply", False)])  # changed: writes
    assert len(writes) > n_after_first
    host.shutdown()


def test_empty_or_no_active_chat_stores_nothing(factory):
    host = _host(factory)
    assert host._remember_conversation(0, "", [_Msg("x", "hi", True)]) is None
    assert host._remember_conversation(0, "Karthik", []) is None
    assert host._remember_conversation(0, None, [_Msg("x", "hi", True)]) is None
    assert host.get_chats_with_memory() == []
    host.shutdown()


def test_memory_accumulates_new_messages_across_views(factory):
    # Viewing a conversation twice must GROW the archive (keep older messages),
    # not replace it with only what's currently on screen — so RAG has history
    # to search. Dedup by (role, text) keeps re-reads from duplicating.
    host = _host(factory)
    _remember_and_wait(host, "Papa", [_Msg("Papa", "one", True), _Msg("Papa", "two", True)])
    _remember_and_wait(host, "Papa", [_Msg("Papa", "two", True), _Msg("Papa", "three", True)])

    texts = [t for _r, _s, t in host.get_chat_memory("Papa")]
    assert texts == ["one", "two", "three"]  # accumulated + deduped, not ["two","three"]
    host.shutdown()
