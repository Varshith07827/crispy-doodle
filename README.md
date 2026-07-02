# winSpark (Python port)

A Python port of [winSpark](../), a Windows window-observation and automation platform
originally written in .NET 8 / WPF. This is a **partial** port — see
[PORT_NOTES.md](PORT_NOTES.md) for exactly what's ported, what was built fresh, what's
deliberately left out, and (importantly) what's verified vs. not.

## What's here

- **SQLite data layer** (`winspark/data/`) — same base schema + fetch-webhook tables as the .NET app
- **Window discovery + event monitoring** (`winspark/engines/`, pywin32) — enumerate windows,
  diff snapshots into open/close/activate/title-change + process start/exit events
- **Rule / automation engine** (`winspark/automation/`) — trigger-indexed rules, conditions,
  actions (log, notify, window actions, text injection), safety policy, STA thread manager,
  UI Automation interaction
- **WhatsApp connector** (`winspark/connectors/whatsapp*.py`) — read the chat list + unread
  state via UI Automation (`GridPattern`, **no OCR**) and send a message into a chat
- **Fetch-Webhook relay** (`winspark/connectors/fetch_webhook_*.py`) — the AI integration
  point: poll an external GET URL (typically an AI service) and relay responses into a
  WhatsApp chat, with dedup, retry, and a local mock server for testing
- **Management CLI** (`winspark/cli.py`) and a **PySide6 desktop UI** (`winspark/ui/`)

## Setup

```powershell
python -m pip install -r requirements.txt
python -m pytest                 # 164 tests; set QT_QPA_PLATFORM=offscreen for the UI tests
```

Most tests run cross-platform; the window/UIA/WhatsApp tests need Windows + pywin32 +
a running WhatsApp Desktop and are skipped elsewhere.

## Running it

```powershell
# Headless engine: discovery + event monitoring + rule engine + fetch-webhook relay
python -m winspark.app

# Manage the fetch-webhook relay (bindings, relay on/off, history, live chats)
python -m winspark.cli bindings list
python -m winspark.cli bindings add "Family" http://localhost:5001/webhook/Family
python -m winspark.cli relay enable
python -m winspark.cli chats

# Desktop control panel (runs the relay engine in-process)
python -m winspark.ui

# Guided end-to-end demo: mock webhook -> real WhatsApp send (asks for confirmation)
python -m scripts.try_fetch_webhook_demo
```

The fetch-webhook relay is **off** unless you enable it (`cli relay enable` or the UI's
Start button) — it won't poll or send anything until then. Point a binding at a real
AI-backed webhook and enabling it turns the loop into: message arrives → your AI generates a
reply → it's sent to the WhatsApp chat.

## Layout

```
winspark/
  domain/       — enums, models, entities
  data/         — SQLite schema, connection factory, repositories
  engines/      — window discovery, event monitoring, window actions, UI Automation, text injection
  automation/   — rule engine, automation engine, registry, safety, STA thread manager
  connectors/   — WhatsApp reader/sender, fetch-webhook relay (client, parser, repo, scheduler, mock server)
  eventbus/     — pub/sub event bus
  ui/           — PySide6 desktop control panel (EngineHost + MainWindow)
  cli.py        — management CLI
  app.py        — headless startup, wires everything together
scripts/        — try_fetch_webhook_demo.py (interactive end-to-end demo)
tests/          — 164 tests (pytest)
```
