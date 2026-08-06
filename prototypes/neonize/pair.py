"""Pair a WhatsApp account with this prototype — run once, first.

Prints a QR code in the terminal. On the phone: WhatsApp -> Settings ->
Linked devices -> Link a device -> scan it. The session is then stored in
session.sqlite3 and every save_*.py script reuses it.

    python pair.py

READ THIS FIRST: this links the account to an unofficial, reverse-engineered
client, which violates WhatsApp's terms and carries a real risk of the number
being restricted or banned. Pair a spare number you can afford to lose, not
your everyday one. winSpark's existing UI-automation path drives the official
WhatsApp Desktop app and does not carry this risk — that's the trade being
evaluated here.
"""

import sys
from pathlib import Path

from neonize.client import NewClient
from neonize.events import ConnectedEv, PairStatusEv, event

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SESSION_DB = Path(__file__).parent / "session.sqlite3"

# See wa_runner.py: the first positional arg IS the database path, and uuid
# must be given separately or it inherits that path.
CLIENT_UUID = "winspark-media-prototype"


def main() -> int:
    already = SESSION_DB.exists()
    print(f"session database: {SESSION_DB}")
    if already:
        print("already paired — connecting to verify (no QR should appear)")
    else:
        print("not paired yet — a QR code will appear below")
        print("the pairing is SAVED to that file, so this is a one-time step")
    print()

    client = NewClient(str(SESSION_DB), uuid=CLIENT_UUID)

    @client.event(ConnectedEv)
    def on_connected(_client: NewClient, _e: ConnectedEv):
        print("\nconnected. Pairing is stored — run a save_*.py script next.")
        print("Ctrl+C to exit.")

    @client.event(PairStatusEv)
    def on_pair(_client: NewClient, e: PairStatusEv):
        print(f"paired as: {e.ID.User}")

    try:
        client.connect()
        event.wait()
    except KeyboardInterrupt:
        print("\nexiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
