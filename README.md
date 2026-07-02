# winSpark (Python port — skeleton)

A partial Python port of [winSpark](../winspark), a Windows window-observation and
automation platform originally written in .NET 8 / WPF. This is a **skeleton**, not
a full port — see [PORT_NOTES.md](PORT_NOTES.md) for exactly what's here vs. missing.

## What works right now

- SQLite schema (`winspark/data/schema.py`) — same tables as the .NET base schema
  (`Applications`, `Events`, `AutomationRules`, `Notifications`, etc.)
- Window discovery via `pywin32` (`EnumWindows`/`GetForegroundWindow`) — **Windows only**
- Event diffing (open/close/activate/title-change, process start/exit) — ported
  algorithm from `EventMonitoringEngine.cs`, verified against it with unit tests
- A minimal async event bus
- `python -m winspark.app` — runs discovery + event monitoring, prints events, persists to SQLite

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # on Windows: .venv\Scripts\pip install -r requirements.txt
.venv/bin/pytest                             # runs cross-platform (no pywin32 needed)
.venv/bin/python -m winspark.app             # Windows only — needs pywin32
```

## Layout

```
winspark/
  domain/     — enums, models, entities (ports of WinSpark.Domain)
  data/       — SQLite schema, connection factory, repositories
  engines/    — window discovery (pywin32), event monitoring (diff algorithm)
  eventbus/   — pub/sub event bus
  services/   — process metrics (psutil)
  app.py      — wires it together, equivalent of the .NET hosted-services startup
```
