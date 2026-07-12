# winSpark

**An AI copilot for the Windows apps you already have open.** winSpark sees your
running apps, reads what's on their screens, answers questions about them, and —
when you ask — *acts* on them for you: clicking, typing, filling forms, sending
messages. Think of it as a Comet-style agent, but for native Windows apps instead
of just the browser.

WhatsApp is the first app with a dedicated, guided integration (auto-replies,
scheduled messages, triggers). Every other app is handled by a generic agent that
drives it through the Windows accessibility layer — no per-app code required.

> Python / PySide6, running on a partial port of the original .NET winSpark engine.
> See [PORT_NOTES.md](PORT_NOTES.md) for exactly what's ported vs. built fresh, and
> what's verified vs. not.

---

## What it does

**See your apps.** A live sidebar lists your open applications — matched to what
Windows itself shows in Task View (background helpers and phantom windows filtered
out; installed web apps like a YouTube PWA appear as their own entry; browser
windows and tabs are selectable).

**Read & ask (any app).** Capture what an app is showing (Windows OCR, plus the app's
accessibility tree for exact text like browser tab names) and ask the AI about it —
"summarise this", "what's the total?", "which tabs are open?". With web lookup on,
answers about current events aren't stuck at the model's training cutoff.

**Act (any app) — the closed-loop agent.** Tell winSpark what to do in plain English
("search for flights to Goa", "reply to the last email"). It works one step at a
time: *look at the app as it actually is now → decide the next single step → do it →
look again* — never assuming what a step "should" have done. It:
- **asks you** when it's genuinely unsure (a choice, a name, your intent) instead of guessing,
- lets you **steer or answer mid-run**, and **Stop** at any time,
- **retries a different way** when a step fails, rather than giving up,
- **remembers what worked** in each app and reuses it next time,
- pauses for your approval before anything risky (send / delete / pay) — or "just do it" mode.

**WhatsApp (dedicated adapter).** Read the chat list and open conversations (groups
included, with real sender names and emoji), send messages, and set up **reply
automations**: reply with AI, post on a schedule, or watch for a message and answer.
A per-chat built-in inbox link is provided — `POST` any text to it and winSpark
forwards it to that chat.

**Automations tab.** Save actions you run often — *send a WhatsApp message* or *do
something in an app* — and run them **on demand**, **on a schedule** ("every morning
at 9"), or **when an app's screen shows something** (literal text or by meaning). A
master **pause switch** stops everything from running on its own.

**Settings.** One place for the AI service: provider (OpenAI / Groq, keys stored per
provider), model, response style (Precise → Creative), and the web-lookup toggle.

**Resilience.** Every automation is snapshotted to a backup file on each change and
restored automatically if the database is ever wiped — so what you programmed can't
vanish across a restart.

---

## Install & run

Requires **Windows** (for the app-automation features), Python 3.11+, and the
dependencies in `requirements.txt`.

```powershell
python -m pip install -r requirements.txt
python -m winspark.ui          # the desktop app
```

First run: open **Settings** (bottom-left) and paste your OpenAI or Groq API key —
that powers AI replies, screen questions, and the acting agent.

### Build a standalone .exe

No Python needed on the target machine:

```powershell
./build_exe.ps1                # -> dist/winSpark.exe
```

Full details, icon replacement, and troubleshooting in [BUILD.md](BUILD.md).

---

## Using it

**Ask or act on an app:** pick an app in the sidebar → choose **Ask about it** or
**Do something** → type your request. For browsers, use the **Window** and **Tab**
dropdowns to target a specific tab.

**A WhatsApp reply automation:** pick **WhatsApp** → choose a chat and **Check chat**
→ pick where replies come from (a web link, AI, or a message trigger) → set how often
to check → **Start automation**. It stays off until you start it.

**A saved automation:** open **⚡ Automations** → **New automation** → choose *Send a
WhatsApp message* or *Do something in an app*, set a trigger (manual / schedule /
when a screen shows text), and save.

### Headless engine & CLI

```powershell
python -m winspark.app                              # engine only: discovery, monitoring, relay
python -m winspark.cli bindings list                # manage WhatsApp reply automations from a terminal
python -m winspark.cli bindings add "Family" http://localhost:5001/webhook/Family
python -m winspark.cli relay enable
```

---

## How it works

- **Engine host** (`winspark/ui/engine_host.py`) runs the engines on a background
  asyncio loop and exposes a small, thread-safe interface to the Qt UI. All UI
  Automation runs on a single dedicated **STA thread** (real apps demand it); heavy
  reads never block the window.
- **App detection** (`winspark/ui/apps.py`, `engines/window_discovery.py`) turns raw
  windows into recognizable apps using Windows' own Task-View rules (DWM cloak +
  window-style + per-window AppUserModelID).
- **The agent** (`winspark/automation/screen_agent.py`) enumerates an app's real
  controls + screen text, asks the AI for one validated step at a time (strict JSON,
  fail-closed), and executes it. Keystrokes use surrogate-pair Unicode so emoji type
  exactly.
- **AI client** (`winspark/connectors/openai_client.py`) speaks the OpenAI
  `/chat/completions` API — the same endpoint covers OpenAI and Groq, and the
  web-search models, by base-URL and model swap.
- **WhatsApp** (`winspark/connectors/whatsapp*.py`) reads a virtualized chat list via
  `GridPattern` and messages via the accessibility tree — **no OCR, no screenshots,
  no cached pixel coordinates**. Transient re-render errors are absorbed and retried.
- **Data** (`winspark/data/`) is SQLite; automations live in the generic
  `AutomationRules` table (action + trigger as JSON), WhatsApp bindings in their own.
- Everything is built on a **generic adapter layer**, so adding another dedicated app
  (Telegram, Outlook, …) is a new adapter, not a rewrite.

### Layout

```
winspark/
  domain/       enums, models, entities
  data/         SQLite schema, connection factory, repositories
  engines/      window discovery, event monitoring, window actions, UI Automation, text injection
  automation/   the acting agent, rule engine, registry, safety, STA thread manager
  connectors/   WhatsApp reader/sender, AI client, fetch-webhook relay + local mock server
  ui/           the desktop app — sidebar shell, panels, engine host, theme, branding
  cli.py        management CLI      app.py  headless startup
scripts/        interactive end-to-end demo
tests/          pytest suite (fakes + stubbed accessibility trees, plus a few live)
```

---

## Testing

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest
```

Most tests run cross-platform against fakes and stubbed accessibility trees. A handful
are **live** — they drive a real WhatsApp Desktop / real windows and are skipped (or
environment-dependent) off Windows or when the app isn't in the expected state.

---

## Honest limits

- **Windows only** for the automation features (they use Windows UI Automation +
  pywin32). The data layer and pure logic run anywhere.
- **The acting agent drives your real mouse and keyboard** unattended when a scheduled
  or triggered app-action fires. It honours the risky-step approval mode and stops on
  repeated failure, but it's real input on the real screen.
- **The built-in WhatsApp inbox link is local** (`localhost`) — reachable only from
  this PC. Paste your own public URL for remote triggering.
- **The WhatsApp chat list only reads what's rendered** (visible chats + a buffer);
  the search fallback covers anything further down.
- **Web-search models cost more** than the standard chat model; winSpark only routes
  a reply to them when the message looks like it needs current information.

See [PORT_NOTES.md](PORT_NOTES.md) for the ported-vs-fresh breakdown and verification
status.
