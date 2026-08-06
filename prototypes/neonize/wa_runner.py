"""Shared plumbing for the per-media-type scripts.

Each save_*.py is the same listener with a different filter, so the connect /
pair / event-wiring lives here once.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable, Optional

from neonize.client import NewClient
from neonize.events import ConnectedEv, MessageEv, PairStatusEv, event

import wa_media

HERE = Path(__file__).parent
SESSION_DB = HERE / "session.sqlite3"
MEDIA_DIR = HERE / "media"

# NewClient's FIRST positional arg is the SQLite path, not a display name — it
# goes straight to the Go layer as the database file (neonize's README shows a
# `database=` kwarg that this version does not have). `uuid` defaults to that
# same string, so it is passed explicitly here: otherwise the client's internal
# identifier becomes a Windows path complete with backslashes and a colon.
CLIENT_UUID = "winspark-media-prototype"

# Windows consoles default to a codepage that cannot encode emoji, and chat
# names are full of them — printing one raises UnicodeEncodeError and kills the
# listener. winSpark hit this exact crash in scripts/try_fetch_webhook_demo.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _describe(meta: dict) -> str:
    bits = [f"{meta['actual_bytes']:,} bytes"]
    if meta.get("seconds"):
        bits.append(f"{meta['seconds']}s")
    if meta.get("width"):
        bits.append(f"{meta['width']}x{meta['height']}")
    if meta.get("page_count"):
        bits.append(f"{meta['page_count']} pages")
    if meta.get("caption"):
        bits.append(f'caption: "{meta["caption"][:40]}"')
    if meta.get("size_mismatch"):
        bits.append("!! SIZE MISMATCH")
    return "  |  ".join(bits)


def run(kinds: Optional[Iterable[str]] = None, *, label: str = "media") -> None:
    """Listen for messages and save any attachment whose kind is in `kinds`
    (all kinds when None). Blocks until Ctrl+C."""
    wanted = set(kinds) if kinds else None
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = NewClient(str(SESSION_DB), uuid=CLIENT_UUID)
    saved = {"n": 0}

    @client.event(ConnectedEv)
    def on_connected(_client: NewClient, _e: ConnectedEv):
        target = ", ".join(sorted(wanted)) if wanted else "every type"
        print(f"connected — watching for {target}")
        print(f"saving into {MEDIA_DIR}")
        print("send something from another phone, then Ctrl+C to stop\n")

    @client.event(PairStatusEv)
    def on_pair(_client: NewClient, e: PairStatusEv):
        print(f"paired as {e.ID.User}")

    @client.event(MessageEv)
    def on_message(client_: NewClient, e: MessageEv):
        media = wa_media.classify(e.Message)
        if media is None:
            return
        if wanted and media.kind not in wanted:
            print(f"skipped a {media.kind} (not this script's job)")
            return
        source = e.Info.MessageSource
        chat = getattr(source.Chat, "User", "") or str(source.Chat)
        sender = getattr(source.Sender, "User", "") or str(source.Sender)
        try:
            meta = wa_media.save_from_event(
                client_, e.Message, MEDIA_DIR,
                chat=chat, sender=sender, message_id=getattr(e.Info, "ID", ""),
            )
        except Exception as ex:  # noqa: BLE001 - one bad file must not stop the listener
            print(f"FAILED to save {media.kind}: {type(ex).__name__}: {ex}")
            return
        saved["n"] += 1
        print(f"[{saved['n']}] {meta['kind']:<10} -> {meta['saved_as']}")
        print(f"     {_describe(meta)}")

    try:
        client.connect()
        event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\nstopped — {saved['n']} file(s) saved under {MEDIA_DIR}")
