"""Interactive demo of the Fetch-Webhook relay: mock webhook -> real WhatsApp send.

This is meant to be run BY YOU, in a terminal, watching your own WhatsApp —
not something automated. It will:
  1. List your real WhatsApp chats (read-only, via UI Automation)
  2. Ask you to pick one and type a message
  3. Ask for explicit confirmation before anything is sent
  4. Create a binding pointed at the local mock webhook server (not a real
     external URL — safe, nothing leaves your machine)
  5. Enable the relay, inject your message into the mock queue, and let the
     scheduler poll and deliver it for real

Run from winspark_py/:  python -m scripts.try_fetch_webhook_demo
"""

from __future__ import annotations

import asyncio
import logging
import sys

from winspark.automation.sta_thread_manager import StaAutomationThreadManager
from winspark.connectors.fetch_webhook_mock_server import WhatsAppFetchLocalMockServer
from winspark.connectors.fetch_webhook_models import FetchWebhookDefaults, WhatsAppFetchBindingEntity
from winspark.connectors.fetch_webhook_relay_service import WhatsAppFetchRelayService
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository
from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler
from winspark.connectors.whatsapp import WhatsAppConnector
from winspark.connectors.whatsapp_group_sender import WhatsAppGroupSender
from winspark.data.connection import ConnectionFactory, default_database_path
from winspark.data.repositories import LogRepository


async def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Chat names can contain emoji; Windows' console defaults to a codepage
    # (e.g. cp1252) that can't encode them — confirmed live (crashed printing
    # a chat name with a heart emoji). Force UTF-8 with a safe fallback.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if sys.platform != "win32":
        print("This demo needs a real Windows session with WhatsApp Desktop running.")
        return

    db_path = default_database_path()
    connection_factory = ConnectionFactory(db_path)
    connection_factory.initialize_schema()

    sta_manager = StaAutomationThreadManager()
    connector = WhatsAppConnector(sta_manager)

    try:
        print("Looking for WhatsApp Desktop...")
        window_handle = await connector.find_window_async()
        if window_handle is None:
            print("WhatsApp Desktop doesn't appear to be running. Start it and try again.")
            return

        rows = await connector.read_chat_rows_async(window_handle)
        if not rows:
            print("No chats found in the visible chat list.")
            return

        print(f"\nFound {len(rows)} chat(s) currently visible in the sidebar:\n")
        for i, row in enumerate(rows[:20], start=1):
            unread = f" ({row.unread_count} unread)" if row.unread_count else ""
            print(f"  {i:2d}. {row.chat_name}{unread}")

        choice = input("\nPick a chat by number (or Ctrl+C to cancel): ").strip()
        try:
            selected = rows[int(choice) - 1]
        except (ValueError, IndexError):
            print("Not a valid choice — aborting, nothing was sent.")
            return

        message_text = input(f"\nMessage to send to '{selected.chat_name}': ").strip()
        if not message_text:
            print("Empty message — aborting, nothing was sent.")
            return

        print(
            f"\nAbout to send this to the REAL chat '{selected.chat_name}' in your WhatsApp:\n"
            f"  \"{message_text}\"\n"
        )
        confirm = input(f"Type the chat name exactly ('{selected.chat_name}') to confirm and send, anything else to cancel: ")
        if confirm.strip() != selected.chat_name.strip():
            print("Confirmation did not match — aborting, nothing was sent.")
            return

        group_sender = WhatsAppGroupSender(connector, sta_manager)
        scheduler = FetchWebhookBindingScheduler()
        mock_server = WhatsAppFetchLocalMockServer()
        relay_service = WhatsAppFetchRelayService(
            WhatsAppFetchRelayRepository(connection_factory),
            LogRepository(connection_factory),
            group_sender,
            mock_server,
            scheduler,
        )

        try:
            binding = WhatsAppFetchBindingEntity(
                group_name=selected.chat_name,
                fetch_url="",  # empty -> normalized to this chat's local mock webhook URL
                poll_interval_seconds=FetchWebhookDefaults.MIN_POLL_INTERVAL_SECONDS,
            )
            await relay_service.save_binding_async(binding)
            await relay_service.set_relay_enabled_async(True)
            await relay_service.inject_test_message_async(selected.chat_name, message_text)

            print("\nQueued. Waiting for the relay to poll and deliver it...")
            for _ in range(15):
                await asyncio.sleep(1)
                messages = relay_service.get_recent_messages(1)
                if messages and messages[0].message_text.strip() == message_text.strip():
                    state = messages[0].state.name
                    print(f"Status: {state}")
                    if state in ("SENT", "FAILED"):
                        break

            binding_status = relay_service.get_bindings()
            for b in binding_status:
                if b.binding_id == binding.binding_id:
                    print(f"\nBinding last state: {b.last_fetch_state!r}  last_error: {b.last_error!r}")

            print("\nDone. Check your WhatsApp to confirm the message actually appeared in the chat.")
        finally:
            await relay_service.set_relay_enabled_async(False)
            scheduler.dispose()
            mock_server.stop()
    finally:
        sta_manager.dispose()


if __name__ == "__main__":
    asyncio.run(main())
