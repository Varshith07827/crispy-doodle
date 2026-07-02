# Port status

Ported faithfully so far (verified by reading the actual .NET source, not guessed):

| .NET file | Python equivalent | Status |
|---|---|---|
| `WinSpark.Infrastructure/Data/DatabaseInitializer.cs` | `winspark/data/schema.py` | Base schema (9 tables) ported 1:1 |
| `WinSpark.Infrastructure/Data/SqliteConnectionFactory.cs` | `winspark/data/connection.py` | Same pragmas (WAL, busy_timeout, foreign_keys) |
| `WinSpark.Infrastructure/Native/WindowEnumerator.cs` | `winspark/engines/window_discovery.py` | `EnumWindows`/`GetWindowText` → `pywin32` equivalents |
| `WinSpark.Infrastructure/Engines/WindowDiscoveryEngine.cs` | `winspark/engines/window_discovery.py` | Discovery loop + process metrics resolution ported |
| `WinSpark.Infrastructure/Engines/EventMonitoringEngine.cs` | `winspark/engines/event_monitoring.py` | Diff algorithm ported line-for-line; unit-tested against the same scenarios |
| `WinSpark.Infrastructure/EventBus/EventBusPublisher.cs` | `winspark/eventbus/publisher.py` | Maps EventEntity → BusEvent with the correct dotted-string EventType |
| `WinSpark.Domain/Interfaces/EventBus/IEventBus.cs` | `winspark/eventbus/bus.py` | Simplified: no per-handler timeout/slow-handler metrics yet |
| `WinSpark.Domain/Models/*`, `Entities/*` (core subset) | `winspark/domain/*` | Only the types needed for discovery/events/automation; see below |
| `WinSpark.Infrastructure/Automation/RuleEngine.cs` | `winspark/automation/rule_engine.py` | Full port: start/stop, CRUD, `evaluate_event_async`, `execute_rule_by_id_async` (depth-limit + cycle detection) |
| `WinSpark.Infrastructure/Automation/AutomationEngine.cs` | `winspark/automation/engine.py` | Full port: action dispatch through safety policy, audit trail recording |
| `WinSpark.Infrastructure/Automation/AutomationRuleMapper.cs` | `winspark/automation/mapper.py` | Entity↔definition + JSON (de)serialization, malformed-JSON fallback |
| `WinSpark.Infrastructure/Automation/AutomationRuleMatcher.cs` | `winspark/automation/matcher.py` | Trigger-parameter filtering + unread gate |
| `WinSpark.Infrastructure/Automation/TriggerIndexedRuleIndex.cs` | `winspark/automation/rule_index.py` | Full port |
| `WinSpark.Infrastructure/Automation/BusEventTriggerMapper.cs` | `winspark/automation/bus_event_trigger_mapper.py` | Full port |
| `WinSpark.Infrastructure/Automation/AutomationComponentRegistry.cs` | `winspark/automation/registry.py` | Full port |
| `WinSpark.Infrastructure/Automation/AutomationSafetyPolicy.cs` | `winspark/automation/safety.py` | Full port: Safe/Moderate/Dangerous levels, allowlist, confirmation-provider hook |
| `WinSpark.Infrastructure/Automation/BuiltIn/BuiltInTriggers.cs` | `winspark/automation/registry.py` | All 7 triggers ported |
| `WinSpark.Infrastructure/Automation/BuiltIn/BuiltInConditions.cs` | `winspark/automation/registry.py` | All 5 conditions ported |
| `WinSpark.Infrastructure/Automation/BuiltIn/BuiltInActions.cs` | `winspark/automation/actions.py` | `LogEventAction`, `ShowNotificationAction`, `ExecuteRuleAction`, all 6 window actions, and `InjectTextAction` all ported. Only `ConnectorSendAction` intentionally NOT ported — see below |
| `WinSpark.Infrastructure/Engines/WindowActionService.cs` | `winspark/engines/window_actions.py` | Full port — plain Win32 calls, no COM/STA needed (see note below) |
| `WinSpark.Infrastructure/Automation/StaAutomationThreadManager.cs` | `winspark/automation/sta_thread_manager.py` | Full port: dedicated STA thread, work queue, health tracking. Needed a fix beyond a literal translation — see below |
| `WinSpark.Infrastructure/Engines/UiAutomationInteractionEngine.cs` | `winspark/engines/ui_automation_interaction.py` | Full port using the `uiautomation` package (comtypes-based) in place of `System.Windows.Automation` |
| `WinSpark.Infrastructure/Engines/TextInjectionEngine.cs` | `winspark/engines/text_injection.py` | Full port: insert/append/replace/clear + (unused, see below) send-enter |
| `CommunicationWindowParser.ExtractUnreadCount` (from `WinSpark.AI`) | `winspark/automation/matcher.py::extract_unread_count` | Pulled in standalone; the rest of `WinSpark.AI` is not ported |
| *(not a port — see below)* | `winspark/connectors/whatsapp.py`, `whatsapp_row_parser.py` | A from-scratch WhatsApp Desktop reader (chat list, unread count, active conversation) via UI Automation only — no OCR, unlike the 39-file .NET connector |
| `FetchWebhookResponseParser.cs` | `winspark/connectors/fetch_webhook_parser.py` | Full port: plain-text/JSON parsing, field-name fallback list, `data` nesting, array-of-candidates |
| `FetchWebhookUrlNormalizer.cs` | `winspark/connectors/fetch_webhook_url.py` | Full port |
| `WhatsAppFetchApiClient.cs` | `winspark/connectors/fetch_webhook_client.py` | Full port — stdlib `urllib` (via `asyncio.to_thread`) instead of `HttpClient`, no new dependency |
| `WhatsAppFetchRelayRepository.cs` | `winspark/connectors/fetch_webhook_repository.py` | Full port, same SQLite schema/semantics |
| `FetchWebhookBindingScheduler.cs` | `winspark/connectors/fetch_webhook_scheduler.py` | Full port — one asyncio task per binding instead of `System.Threading.Timer`, same staggered-start + concurrent-tick-skip behavior |
| `WhatsAppFetchLocalMockServer.cs` | `winspark/connectors/fetch_webhook_mock_server.py` | Core endpoints ported (`webhook/{group}`, `api/inject[/​{group}]`, `api/status`, `api/queue/{group}`); batch-inject variants not ported |
| `WhatsAppFetchRelayService.cs` | `winspark/connectors/fetch_webhook_relay_service.py` | Full orchestration port: poll → parse → dedupe (external id / content hash) → persist → send → retry with backoff up to `MaxSendAttempts` |
| *(not a port — see below)* | `winspark/connectors/whatsapp_group_sender.py` | A from-scratch group sender using the GridPattern chat-row finder instead of the .NET version's OCR+visual sidebar-coordinate binding |

### Why WindowActionService didn't need the STA/COM treatment, but UI Automation did

Window actions (bring-to-front, minimize, close, etc.) are plain User32 calls
(`SetForegroundWindow`, `ShowWindow`, `SendMessage(WM_CLOSE)`) — the .NET version only
marshals them onto an STA thread because its **WPF UI thread** requires it, not because
Win32 itself does. `winspark/engines/window_actions.py` calls `win32gui` directly with no
COM involved.

UI Automation is different: it's genuinely COM, and genuinely needs a dedicated,
consistently-pumped STA thread. Two real problems surfaced while building and testing
`StaAutomationThreadManager`/`UiAutomationInteractionEngine`, neither obvious from reading
the C# source alone:

1. **A literal translation of the STA thread loop deadlocks.** The .NET version's worker
   thread blocks on `BlockingCollection.GetConsumingEnumerable()` between work items — a
   plain translation to `queue.Queue().get()` (no timeout) does the same, and it hangs the
   first time a UI Automation call needs the target window's owning thread to respond to a
   message (confirmed by reproducing this for real: the target test window went **"Not
   Responding"** while `SetFocus()` sat blocked). .NET's WPF/WinForms message pump almost
   certainly pumps messages incidentally elsewhere in the process, masking this. The fix in
   `sta_thread_manager.py` is to poll the queue with a short timeout and call
   `pythoncom.PumpWaitingMessages()` between checks, so the STA thread keeps servicing
   Windows messages even while idle.
2. **Rapidly creating/disposing multiple `StaAutomationThreadManager` instances in one
   process is unsafe.** Doing this in a test loop threw `RPC_E_DISCONNECTED` (HRESULT
   `0x80010108`) from inside `uiautomation`'s `SetFocus` — the `uiautomation` package caches
   COM state at module (process) scope, and tearing down the STA apartment that created it
   out from under a second instance corrupts that cache. This isn't just a test artifact:
   it means the real app must treat `StaAutomationThreadManager` as a true singleton created
   once at startup, exactly as the .NET DI container already does by registering it as a
   singleton service — this constraint just wasn't visible from reading the interface alone.

### A real, dormant bug found in the .NET original (not a Python gap)

`BuiltInActions.cs`'s `InjectTextAction` sets `SendEnterAfter` on the `TextInjectionRequest`
it builds, but `TextInjectionEngine.cs` never reads that field anywhere — grepped the whole
solution to confirm. So in the .NET app today, the "send Enter after" option on a text
injection action is silently a no-op. `winspark/automation/actions.py::InjectTextAction`
reproduces this exactly (sets the field, never acts on it) rather than "fixing" behavior the
original app doesn't actually have — that's not this port's call to make.

### A real, verified limitation of SetControlTextAsync (present in both versions)

Both the .NET and Python `SetControlTextAsync` only work through UI Automation's
`ValuePattern`. This was verified against a live example while building this port: Windows
11's current WinUI-based Notepad exposes its text surface as a `DocumentControl` /
`RichEditD2DPT` control with no `ValuePattern` support (it's a `TextPattern`-only rich-text
surface), so `InjectTextAction` cannot type into it — in *either* language. This is a
limitation of the feature as designed, not a porting gap; the tests in
`test_ui_automation_interaction.py`/`test_text_injection.py` deliberately target a
self-created native Win32 `EDIT` control (which does support `ValuePattern`) rather than a
real app, both to avoid touching the user's windows and because it's the actual class of
control this action supports.

### The WhatsApp connector: not a port, built fresh — and it doesn't need OCR

The real .NET WhatsApp connector (`Infrastructure/Connectors/WhatsApp/`) is ~39 files built
around a hybrid OCR (`Windows.Media.Ocr`) + visual-detection + UI-Automation strategy, with a
certification/retry/analytics layer on top — a multi-day undertaking on its own, not
attempted here. Instead, `winspark/connectors/whatsapp.py` was built from scratch after
inspecting a real, running WhatsApp Desktop window directly:

- A plain UI Automation tree walk (`GetChildren()`) on the "Chat list" control returns **zero
  rows** — it looks empty/virtualized.
- But `GridPattern.GetItem(row, 0)` on that same control **does** return rows, each with the
  contact name, last message preview, timestamp, and unread count all concatenated into the
  row's accessible Name (e.g. `"4 unread messages Vishnu Cr Gvp Yesterday ekada grp names..."`).
  No screenshot, no OCR confidence score, works even if the window is occluded. This is a
  standard UI Automation technique for virtualized lists that the .NET codebase doesn't
  appear to use.
- **Caveat found by actually testing this against real data, not assumed:** `GridPattern`
  reported `RowCount=511` for a real chat list, but `GetItem()` only succeeds for rows
  Chromium has *realized* near the current scroll position — it threw `COMError`
  (`E_INVALIDARG`) around row 68 in one run. So this reads whatever's currently rendered
  (visible chats plus a nearby buffer), not the entire chat history; scrolling further would
  be needed to walk deeper. `read_chat_rows_async` stops cleanly at the first unrealized row
  rather than raising.
- The per-row text has no field delimiters, so `whatsapp_row_parser.py`'s `parse_chat_row` is
  a tuned heuristic (leading "N unread messages" prefix → trailing flag phrases like
  "Pinned chat"/"Starred chat" → a day/time "anchor" that splits chat name from message) —
  tested against 5 real rows captured live, not invented ones.
- The unread badge count is read from the "Unread" tab item's own accessible name (e.g.
  `"Unread 5"`) rather than the window title — on this WhatsApp install the top-level window
  title is just `"WhatsApp"` with no count, so the title-regex approach
  (`extract_unread_count`) that works for the C# app's assumption doesn't apply here.
- `test_whatsapp_connector.py` is a read-only smoke test against whatever WhatsApp Desktop
  instance is actually running (skips if none is) — it never launches, closes, or sends
  anything, unlike the synthetic-window tests elsewhere in this suite.
- Sending a message (`winspark/connectors/whatsapp_group_sender.py`) is implemented, using
  `GridPattern` again to find the target chat row (so `Control.Click()` clicks the row's real
  bounding rect — no cached pixel coordinates) and real simulated keystrokes for the compose
  box. See below for why "real keystrokes" specifically, and not the existing
  `TextInjectionEngine`/`ValuePattern` path that works fine for native controls.

### Real dead end found live: WhatsApp's compose box doesn't work like a native text control

The original assumption — reuse `TextInjectionEngine`'s `ValuePattern.SetValue()` approach,
since the compose box's accessible Name (`"Type a message to {contact}"`) and pattern support
looked identical to a normal text box — turned out to be wrong, and only testing against the
real, running app caught it:

- `ValuePattern.SetValue("some text")` returns successfully (no exception), but
  `ValuePattern.Value` reads back as a static `'\n'` both before and after — the text never
  actually lands. WhatsApp's compose box is a React-managed `contenteditable` div; writing
  through the accessibility `Value` property doesn't touch React's own state, so the UI
  silently ignores it.
- The fix: simulate real keystrokes (`uiautomation.SendKeys`) instead of `ValuePattern`, and
  read the result back via `TextPattern.DocumentRange.GetText(-1)` instead of `ValuePattern.Value`
  (which stays a useless `'\n'` for read purposes too — an empty box and a box holding real
  text are indistinguishable through `ValuePattern`, but `TextPattern` reads correctly for both).
- `SendKeys` routes to whatever window Windows considers the **actual OS foreground window**,
  not whatever element UI Automation's `SetFocus()` was called on. Without explicitly calling
  `win32gui.SetForegroundWindow()` first, keystrokes silently went to the test's own terminal
  window instead of WhatsApp — everything looked like it succeeded (no exceptions) but nothing
  was typed. Confirmed by testing from a real terminal, where the failure mode is invisible
  without checking the actual destination.
- Also found: the `uiautomation` package's own `SendKeys("")` raises an uncaught `IndexError`
  for an empty string — worked around by skipping the call entirely when there's nothing to
  type (clearing existing content via `{Ctrl}a{Delete}` already covers that case).
- **Deliberately not tested by an automated send.** `test_whatsapp_group_sender.py` verifies
  every step up to (not including) pressing Enter — resolving a real chat by name, clicking it
  open, confirming the compose box updated, typing a test string, and clearing it again —
  against a real, running WhatsApp instance, without ever actually delivering a message to a
  real contact. Actually sending should be a deliberate, user-initiated action, not something
  an automated test run does on its own.

## The Fetch-Webhook relay: this port's actual "AI" integration point

`winspark/connectors/fetch_webhook_*.py` + `whatsapp_group_sender.py` port the feature
described in the .NET repo's README as its "main workflow": bind a WhatsApp chat to an
external GET URL (typically an AI service), poll it on an interval, and relay any non-empty
response into that chat. winSpark doesn't call an LLM itself — it's a relay, and this is
where an external AI service plugs in.

Ported: the response parser (plain text / JSON field extraction / nested `data` / arrays),
URL normalizer, an async HTTP client (stdlib `urllib`, no new dependency), the SQLite
binding/message repository, a per-binding async polling scheduler, a local mock HTTP server
for testing without a real external webhook, and the full poll → parse → dedupe → persist →
send → retry orchestration. Not ported: the batch-inject mock-server endpoints (lower value,
not part of the documented core workflow) and the Monitor Activity Service (a UI activity feed,
not needed for the feature to function).

Verified end-to-end with real HTTP round-trips (a real local server, not mocked), a real
SQLite database, and real `asyncio` scheduling — with a stub group sender standing in for the
real WhatsApp send, for the same reason described above: an automated test suite should never
autonomously deliver a message to a real contact. Wired into `app.py` but left disabled by
default (no bindings exist on a fresh install, and nothing calls `set_relay_enabled_async`) —
enabling it and pointing it at a real AI backend is a decision for whoever runs the app, not
something this port turns on by default.

## Front ends: a management CLI and a desktop UI (both fresh, not ports)

The C# app is a WPF desktop application (`WinSpark.App`, 57 files). Rather than port that
XAML/MVVM UI, two purpose-built Python front ends were added over the relay + engine:

- **`winspark/cli.py`** (`python -m winspark.cli`) — manage fetch-webhook bindings
  (add/list/enable/disable/remove), inspect relayed-message history, toggle the relay
  on/off (persisted to a `Settings` row the app reads at startup), and list live WhatsApp
  chats. Binding/message/relay commands are pure SQLite and run on any platform; only
  `chats` needs Windows. `app.py` now reads the persisted relay-enabled flag on boot, so
  `cli relay enable` actually takes effect when the app next starts.
- **`winspark/ui/`** (`python -m winspark.ui`) — a PySide6 desktop control panel. It runs
  the relay engine *in-process* on a background asyncio thread (`EngineHost`), so toggling
  the relay on begins polling and relaying for real; the window manages bindings, injects
  test messages, and shows message history refreshed live on a timer. Qt can't share
  asyncio's loop, so the engine runs on its own thread and the Qt thread submits coroutines
  via `run_coroutine_threadsafe` and reads plain SQLite state directly. The window depends
  only on a small duck-typed controller, so its logic is tested headless (Qt `offscreen`
  platform) against a fake, and the real `EngineHost` was separately smoke-tested (background
  loop, relay enable/disable across the thread boundary, clean shutdown, no orphaned process).
  PySide6 is an optional dependency — the CLI, headless app, and relay all run without it.

### Another real Windows finding: SetForegroundWindow is advisory, not reliable

Surfaced when the full suite ran in a background (non-foreground) process: two live tests
failed because `win32gui.SetForegroundWindow` **raised** (error code 0, blank message).
Windows refuses programmatic foreground changes from a process that isn't already the
foreground process — a documented anti-focus-stealing restriction. The .NET original calls
the Win32 API and ignores its `BOOL` return; pywin32 instead raises, and the code was letting
that fail the whole operation. Fixed by making every `SetForegroundWindow` call best-effort
(swallow the refusal) in both `window_actions.py` and `whatsapp_group_sender.py` — the real
foreground transfer for automation comes from synthesized **mouse clicks** (which Windows
honors as genuine user input), not from this advisory call. Confirmed stable across repeated
full-suite runs afterward. This is a genuine robustness fix, not a test workaround: window
activate/bring-to-front should not hard-fail just because Windows declined to steal focus.

## Deliberately not ported yet (by size, in the C# solution)

The .NET `WinSpark.Domain` project alone has 248 files. Everything below is real,
substantial work still ahead of a full migration:

- **`WinSpark.AI`** (77 files, ~11k lines) — the WhatsApp/Slack/Teams/Outlook/Browser
  "communication agents", meeting/conversation analysis, workflow detection, AI gateway. A
  small fresh WhatsApp reader was added separately (see above) — this is not a port of any of
  WinSpark.AI's files, and Slack/Teams/Outlook still have nothing.
- **The real `Infrastructure/Connectors/WhatsApp/` connector** (39 files: OCR reading, visual
  fingerprinting, certification/retry/analytics) — a fresh reader + sender were built instead
  (see above), covering reading the chat list/unread state and sending a message, but not the
  OCR/visual-fallback reliability layer or the certification/analytics infrastructure.
- **`WhatsAppMonitorActivityService`** (the newest .NET commit's UI activity feed) — not
  ported; it's a display concern for the WPF activity panel, not needed for the Fetch-Webhook
  relay to function. `fetch_webhook_relay_service.py` logs the same events via the standard
  `Logs` table/repository instead.
- **`ConnectorSendAction`** — depends on the `WinSpark.AI` connector layer; left
  unregistered in `register_builtin_actions` rather than stubbed, since a fake stub would
  silently no-op at rule-execution time instead of failing loudly at rule-authoring time.
- **`IWindowIntelligenceEngine`** and other higher-level UIA consumers (window profiling,
  control-tree caching for AI features) — the low-level `IUiAutomationInteractionEngine` this
  builds on is now ported, but nothing above it is.
- **Plugin SDK + sample plugins** — `IEventProvider`/`IRuleProvider`/`IWindowAnalyzer`/`IActionProvider`
  contracts and dynamic loading from a `plugins/` folder.
- **Retention service**, **schema migrations** (.NET is at schema version 17; this port
  only has the version-1 base schema — AI/connector/WhatsApp tables aren't here).
- **WPF UI** (`WinSpark.App`, 57 files) — not ported as such. A fresh PySide6 control panel
  (`winspark/ui/`) covers the fetch-webhook relay; the many other WPF panels (events, rules
  editor, window inspector, automation catalog, etc.) have no Python equivalent yet.
- **WhatsApp-specific window handling** (`EnsureWhatsAppWindowListed`,
  `WhatsAppCaptureHandleResolver`) — skipped in this port's `window_discovery.py`.
- **`DefaultRulesSeeder`, `DraftAutomationRuleConverter`** — seed-data and AI-suggested-rule
  conversion helpers; not needed for the engine to function, not ported.

## Verified, not just written

Ran `pytest` (164 tests, all passing; run with `QT_QPA_PLATFORM=offscreen` for the UI tests) covering:
- schema creation produces all 9 expected tables
- event/application/snapshot repository round-trips
- the event-diffing algorithm reproduces the .NET engine's behavior for:
  window-opened+activated, title-changed, window-closed+process-exited, and
  the no-op case (stable window between snapshots)
- **window discovery smoke test**, run on a real Windows session with pywin32 installed.
  Caught a real bug: `win32gui.IsZoomed` doesn't exist in pywin32 — fixed via
  `GetWindowPlacement`. Discovery correctly enumerates live windows with correct process
  names, memory metrics, active-window detection, and minimized/maximized/normal state.
- **rule mapper, matcher, trigger index**: entity↔definition round-trips (malformed/empty
  JSON degrades to empty values instead of raising), the `extract_unread_count` regex ladder
  against real-looking window titles, enabled-only case-insensitive trigger indexing.
- **rule engine end-to-end**: a real `RuleEngine` wired to a real `AutomationComponentRegistry`,
  a real `AutomationEngine`, and a real SQLite database — a rule fires on a matching bus
  event and writes to both `Logs` and `AutomationAuditTrail`, does *not* fire on a
  non-matching trigger or when disabled, condition filtering excludes non-matching
  processes, and `execute_rule_by_id_async` enforces the depth limit (8) and cycle detection.
- **automation engine + safety policy**: unregistered actions fail cleanly, a Dangerous
  action (close-window) is blocked when confirmation is required and no confirmation
  provider is registered, allowed once the requirement is disabled, and `execute_rule_async`'s
  overall success is the AND of all action results.
- **notification message-building**: default message templates per event type, custom-message
  override, and the "skip if rule implies unread but title has no unread count" gate — the
  real Win32 balloon-tip call is monkeypatched out so this runs on any platform.
- **window actions smoke test**, Windows-only: invalid handle rejected cleanly;
  `activate`/`bring_to_front` against a real window both succeed.
- **STA thread manager**, Windows-only: work reliably marshals onto a single dedicated
  thread (verified by comparing `threading.get_ident()` across calls and against the
  caller), a failing work item raises to the caller without breaking the queue for
  subsequent items, and `dispose()` rejects further work.
- **UI Automation interaction + text injection**, Windows-only: find-by-`ClassName`,
  set/read text via `ValuePattern`, focus, and button click (`InvokePattern`) all verified
  against a native `EDIT`/`BUTTON` control pair created and destroyed entirely within the
  test process — deliberately never touches a real application window. `TextInjectionEngine`
  verified end-to-end for replace/insert(-append)/clear, including a Win32-level
  `GetWindowText` readback independent of the UIA layer that wrote it, and a clean failure
  path when the target control doesn't exist.
- **full app boot smoke test** (manual, not in the suite): ran `python -m winspark.app`
  against the real desktop with discovery + event monitoring + rule engine + the STA/UIA
  stack all wired together — started cleanly (STA thread confirmed started in the logs),
  processed real window-open/process-started events, no exceptions, clean shutdown.
- **WhatsApp row parser**, cross-platform: 7 tests against 5 real chat-list rows captured
  live (unread-count prefix, trailing pinned/starred flags in combination, URL-containing
  messages, no-anchor fallback).
- **WhatsApp connector smoke test**, Windows-only, read-only, against whatever WhatsApp
  Desktop instance is actually running: finds the real window, reads the unread badge count,
  reads real chat rows (contact names, previews, unread counts) via `GridPattern`, confirms
  unread chats are a subset of all chats, and confirms the badge count is at least what's
  visible in the currently-realized row range. Caught a real bug in the first run: `GetItem()`
  raised `COMError` past row ~68 of a 511-row list — fixed by stopping cleanly at the first
  unrealized row instead of assuming the whole logical list is walkable (see the WhatsApp
  connector section above).
- **WhatsApp group sender**, Windows-only, against real running WhatsApp: resolves a real
  chat by name via `GridPattern`, returns `None` for a nonexistent chat, and — the real proof
  this works — opens a real chat, confirms the compose box updated to the new conversation,
  types a test string, verifies via `TextPattern` that it actually landed (catching the
  `ValuePattern` dead-end described above), then clears it. Never presses Enter; see above for
  why an automated test shouldn't be the thing that sends a real message.
- **Fetch-Webhook response parser + URL normalizer**, cross-platform, no Windows dependency:
  plain text, JSON field-name fallback (`message`/`text`/`content`/`body`/`msg`), `data`
  nesting (object or string), arrays-of-candidates, invalid JSON, and the URL-paste-mistake
  fixups (leading `GET`/`POST`, pasting the inject URL instead of the poll URL).
- **Fetch-Webhook HTTP client**, cross-platform: real request/response round-trips against a
  real local HTTP server (not mocked) — plain text, JSON, HTTP 204, HTTP error statuses,
  Bearer auth header.
- **Fetch-Webhook repository**, cross-platform, real SQLite: binding CRUD, upsert-as-update,
  cascading delete, status transitions (including the `send-failed` clears-`LastSendUtc`
  quirk from the C# original), poll/sent counters, message dedup by content hash, in-flight
  detection, and the retryable-messages query respecting `NextRetryUtc`/max attempts.
- **Fetch-Webhook scheduler**, cross-platform, real `asyncio` timing (with a monkeypatched
  tiny interval floor so tests run in ~2s instead of the real 3-second production minimum):
  concurrent ticks for the same binding are skipped rather than queued, only enabled bindings
  poll when the relay is on, suspend/resume pauses and resumes ticking without killing the
  underlying task, and `stop_binding` actually stops it.
- **Fetch-Webhook local mock server**, cross-platform, real HTTP requests via `urllib` (no
  mocking): destructive-read GET semantics, FIFO ordering, round-robin injection across
  configured groups, `api/status`/`api/queue` reporting, and proper 404/405s.
- **Fetch-Webhook relay orchestrator end-to-end**, cross-platform: a real service wired to
  the real mock server + real SQLite + real scheduler (stub group sender standing in for a
  real WhatsApp send) — poll-and-relay, external-id dedup skipping an already-sent message,
  retry-then-succeed, permanent failure after `MaxSendAttempts`, full enable-flow polling
  automatically via the scheduler, disabled bindings not polling, and pause/resume.
- **Management CLI** (16 tests), cross-platform: drives `cli.main(argv)` against a temp DB —
  binding add (with URL normalization + validation), add-same-group-updates-not-duplicates,
  enable/disable/remove by group name or BindingId, unknown-binding error, message history
  display, and relay enable/disable persisting the exact settings key the app reads at boot.
  The `chats` command was also smoke-tested live against real WhatsApp (read all 28 chats).
- **Desktop UI** (11 tests), headless via Qt's `offscreen` platform: the PySide6 window's
  logic driven against a fake controller — empty-state, add-binding populates the table,
  relay toggle flips state + button label, enable/disable/remove selected, no-op on empty
  selection, send-test forwards to the controller, message-history rendering, same-group
  update-in-place, and the add-binding dialog collecting field values. The real `EngineHost`
  (background asyncio loop + relay engine in-process) was separately smoke-tested offscreen:
  relay enable/disable across the thread boundary and clean shutdown with no orphaned process.

### Known deviation fixed during this pass (Python-side bug, not in the .NET original)

`EventMonitoringEngine.py` was publishing `BusEvent.event_type` as the Python enum member
name (`"WINDOW_OPENED"`) instead of the dotted string constant (`"window.opened"`) that
`BusEventTriggerMapper` looks up. This meant no rule could ever have matched a live event —
the rule engine would have silently never fired. Fixed by porting `EventBusPublisher.cs`
into `winspark/eventbus/publisher.py` and routing `event_monitoring.py` through it.

### Two more real bugs found running the full suite back-to-back (not on first pass)

Both surfaced only when running all 137 tests together, not in isolation — worth recording
since "passes alone" isn't the same as "correct":

1. **`StaAutomationThreadManager` had a genuine race condition.** The worker thread called
   `item.future.set_result(result)` *before* incrementing `_total_processed`. `set_result()`
   wakes the awaiting coroutine via `call_soon_threadsafe`, which can resume and read
   `get_health()` before the STA thread executes its very next line — confirmed live: a test
   awaited a successful call, then immediately asserted `total_requests_processed >= 1` and
   got `0`. Fixed by updating the counters before calling `set_result()`.
2. **The WhatsApp compose box's React re-render lags slightly behind the keystroke that
   triggers it.** `_set_compose_text_sync`'s clear-and-verify step (`Ctrl+A`, `Delete`, then
   immediately read back via `TextPattern`) intermittently read the *old* text — not
   consistently, only under load, which is why it didn't show up until running the whole
   suite. Fixed with a short settle delay before the verifying read.
3. **The "one STA manager per test module" fix from earlier wasn't enough once there were 4
   UIA-touching test files instead of 2.** Each file still created its own
   `StaAutomationThreadManager`, so running the whole suite chained 4 separate STA-thread
   create/dispose cycles back to back — enough to reproduce the same `RPC_E_DISCONNECTED`
   COM corruption again, just at the cross-file level instead of cross-test. Fixed by moving
   the `manager` fixture to a session-scoped one in `tests/conftest.py`, shared by every UIA
   test file — one STA thread for the entire test run, matching how the real app uses it.
   Confirmed clean (no crash dump) across repeated full-suite runs after the fix.

### Demo script: `scripts/try_fetch_webhook_demo.py`

An interactive script (not a test) that lists your real WhatsApp chats, asks you to pick one
and type a message, requires you to type the chat's name back as an explicit confirmation,
then runs the full loop — mock webhook → poll → real WhatsApp send — and reports the result.
Deliberately requires a human at the keyboard for the confirmation step; nothing in the
automated test suite triggers a real send. Also fixed a real crash here: printing a chat name
containing an emoji raised `UnicodeEncodeError` against Windows' default console codepage —
fixed by reconfiguring stdout/stderr to UTF-8 with `errors="replace"`.
