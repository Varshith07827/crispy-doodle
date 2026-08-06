# neonize media prototype

Evaluating [neonize](https://github.com/krypton-byte/neonize) as a replacement
for winSpark's UI-automation WhatsApp connector — specifically for the thing
the current approach **cannot do at all**: getting the actual bytes of an
attachment.

winSpark today drives WhatsApp Desktop through the Windows accessibility tree,
which only ever *names* an attachment. From `winspark/constants.py`:

> Save on-screen thumbnails of photo/sticker/GIF messages to disk (the only way
> to any image pixels — WhatsApp never exposes the bytes through the
> accessibility tree). […] Voice notes and the original-resolution files are
> never obtainable this way.

neonize wraps [whatsmeow](https://github.com/tulir/whatsmeow) and speaks the
multi-device protocol directly, so `client.download_any(message)` returns the
real file — original quality, including voice notes and documents.

---

## ⚠️ Before you pair anything

This links a WhatsApp account to an **unofficial, reverse-engineered client**.
That violates WhatsApp's terms and carries a real risk of the number being
restricted or banned. **Pair a spare number, not your everyday one.**

winSpark's existing UI-automation path drives the *official* WhatsApp Desktop
app, which does not carry this risk. That is the trade this prototype exists to
evaluate — slow, blind-to-media, but safe, versus fast, full-media, but against
the rules.

---

## What's verified, and what isn't

Everything below ran on this machine:

| Checked | Result |
|---|---|
| `pip install neonize` on Windows/Python 3.13 | works, 21 deps |
| Native Go library | **bundled in the wheel** — `neonize-windows-amd64.dll`, 20.7 MB. No runtime download. |
| DLL loads via ctypes | yes, 0.40s import |
| `download_any` on sync + async clients | both present |
| Media classify → name → save pipeline | 21 tests pass |
| Real files written and re-opened | JPEG, WebP, PDF all valid |

**Not verified:** the live `download_any()` network call, because that needs a
paired account. Every other line of the pipeline is covered offline.

The bundled DLL matters for `BUILD.md`: it's a plain data file, so PyInstaller
needs a `datas` entry in `winspark.spec`, not a runtime download step.

---

## Setup

```powershell
cd prototypes\neonize
.venv\Scripts\python.exe check_binary.py     # confirm the native layer loads
.venv\Scripts\python.exe pair.py             # scan the QR with a SPARE phone
```

`pair.py` prints a QR code. On the phone: **WhatsApp → Settings → Linked
devices → Link a device**. The session is stored in `session.sqlite3` and every
script below reuses it.

## Testing media saving

One script per media type, so you can test them in isolation:

```powershell
.venv\Scripts\python.exe save_images.py       # photos
.venv\Scripts\python.exe save_audio.py        # voice notes + music
.venv\Scripts\python.exe save_videos.py       # video, GIF, round video notes
.venv\Scripts\python.exe save_documents.py    # pdf, docx, zip, ...
.venv\Scripts\python.exe save_stickers.py     # webp, animated
.venv\Scripts\python.exe save_all.py          # everything at once
```

Each runs until Ctrl+C. Send the matching media from another phone and watch it
land. Files go to `media/<kind>/`, each with a `.json` sidecar:

```
media/image/20260803-143000-Nagen_US-image-DEMO0000ABCD.jpg
media/image/20260803-143000-Nagen_US-image-DEMO0000ABCD.json
media/voice/20260803-143021-Nagen_US-voice-DEMO0003ABCD.ogg
media/voice/20260803-143021-Nagen_US-voice-DEMO0003ABCD.json
```

Names are `timestamp-chat-kind-messageid`: sortable, safe on Windows (emoji and
`/` stripped out of chat names), and unique per message so the same file sent
twice never overwrites itself.

## Without pairing

```powershell
.venv\Scripts\python.exe demo_save.py                    # writes real files into media/
.venv\Scripts\python.exe -m pytest test_wa_media.py -v   # 21 offline tests
```

`demo_save.py` runs the entire pipeline with the network call stubbed, and
produces genuinely valid JPEG/WebP/PDF files you can open.

---

## What each file is

| File | Purpose |
|---|---|
| `wa_media.py` | The reusable core: classify a message's media, name it, write it + sidecar. No live client — pure functions over protobufs. |
| `wa_runner.py` | Shared connect/pair/event wiring for the `save_*.py` scripts. |
| `save_*.py` | One per media type; each is a filter over the same runner. |
| `pair.py` | One-time QR pairing. |
| `check_binary.py` | Proves the native layer loads before you pair. |
| `demo_save.py` | Full pipeline offline, writes real files. |
| `test_wa_media.py` | 21 offline tests over hand-built protobufs. |
| `introspect.py` | Throwaway: dumps the protobuf fields this was written against. |

## Media types handled

`image`, `video`, `video_note` (round `ptvMessage`), `audio`, `voice` (PTT flag
— a recorded voice note is separated from a shared music file), `document`,
`sticker`. Wrapper messages are unwrapped, so view-once and
document-with-caption still resolve to their real media.

WhatsApp's declared `fileLength` is compared against the bytes actually
written; a mismatch is recorded as `"size_mismatch": true` in the sidecar
rather than leaving a silently truncated file on disk.

## If it graduates

The integration point in winSpark is small. The relay only calls four methods
on the sender (`send_to_group_async`, `read_recent_incoming_async`,
`read_last_incoming_async`, `read_last_incoming_message_async`), chosen in one
place — `_build_group_sender()` in `winspark/ui/engine_host.py`. A neonize
adapter implementing those four would drop in without touching the relay,
`!winspark` command mode, chat memory or the UI.

`wa_media.py` would then feed `WhatsAppChatMemory.MediaPath`, which already
exists in the schema for exactly this.
