"""Management CLI for winSpark's Fetch-Webhook relay (the AI integration point).

Manage the WhatsApp <-> webhook bindings, inspect relayed messages, and flip
the relay on/off — without editing the SQLite database by hand or running the
demo script. Operates on the same database the app uses
(%LOCALAPPDATA%/winSpark/winspark.db by default; override with --db).

Examples:
    python -m winspark.cli bindings list
    python -m winspark.cli bindings add "Family" http://localhost:5001/webhook/Family --interval 5
    python -m winspark.cli bindings disable "Family"
    python -m winspark.cli bindings remove "Family"
    python -m winspark.cli messages --limit 10
    python -m winspark.cli relay status
    python -m winspark.cli relay enable
    python -m winspark.cli chats            # Windows + running WhatsApp only

Binding CRUD and message/relay inspection work on any platform (pure SQLite).
`chats` needs a real Windows session with WhatsApp Desktop running.

Note: the running app reads the relay-enabled flag and its bindings from this
same database at startup and when it re-syncs — changes made here while the
app is already running may not take effect until it next syncs or restarts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from winspark.connectors.fetch_webhook_models import FetchWebhookDefaults, WhatsAppFetchBindingEntity
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository
from winspark.connectors.fetch_webhook_url import normalize_poll_url, try_validate_poll_url
from winspark.constants import SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED
from winspark.data.connection import ConnectionFactory, default_database_path
from winspark.data.repositories import SettingsRepository


def _resolve_binding(repo: WhatsAppFetchRelayRepository, identifier: str) -> Optional[WhatsAppFetchBindingEntity]:
    """Match a binding by exact BindingId first, then by group name (case-insensitive)."""
    exact = repo.get_binding(identifier)
    if exact is not None:
        return exact
    target = identifier.strip().lower()
    matches = [b for b in repo.get_bindings() if b.group_name.strip().lower() == target]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(f"'{identifier}' matches {len(matches)} bindings by group name; use the BindingId instead.")
    return None


def _relay_enabled(settings: SettingsRepository) -> bool:
    value = settings.get_value(SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED)
    return value is not None and value.lower() in ("true", "1")


def _cmd_bindings_list(repo: WhatsAppFetchRelayRepository) -> int:
    bindings = repo.get_bindings()
    if not bindings:
        print("No bindings. Add one with:  bindings add <group> <url>")
        return 0

    print(f"{'ENABLED':<8} {'GROUP':<24} {'INTERVAL':<9} {'STATE':<14} {'POLLS':<6} {'SENT':<5} URL")
    print("-" * 100)
    for b in bindings:
        enabled = "yes" if b.is_enabled else "no"
        print(
            f"{enabled:<8} {_clip(b.group_name, 24):<24} {str(b.poll_interval_seconds) + 's':<9} "
            f"{_clip(b.last_fetch_state, 14):<14} {b.total_polls:<6} {b.total_sent:<5} {b.fetch_url}"
        )
        print(f"         id={b.binding_id}" + (f"   last_error={b.last_error}" if b.last_error else ""))
    return 0


def _cmd_bindings_add(repo: WhatsAppFetchRelayRepository, args) -> int:
    group = args.group.strip()
    if not group:
        print("Group name cannot be empty.", file=sys.stderr)
        return 2

    poll_url = normalize_poll_url(args.url, group)
    ok, error = try_validate_poll_url(poll_url)
    if not ok:
        print(f"Invalid poll URL: {error}", file=sys.stderr)
        return 2

    existing = next((b for b in repo.get_bindings() if b.group_name.strip().lower() == group.lower()), None)
    binding = WhatsAppFetchBindingEntity(
        binding_id=existing.binding_id if existing else WhatsAppFetchBindingEntity().binding_id,
        group_name=group,
        fetch_url=poll_url,
        api_key=args.api_key or "",
        poll_interval_seconds=max(FetchWebhookDefaults.MIN_POLL_INTERVAL_SECONDS, args.interval),
        is_enabled=not args.disabled,
    )
    repo.upsert_binding(binding)
    verb = "Updated" if existing else "Added"
    print(f"{verb} binding for '{group}':")
    print(f"  poll URL : {poll_url}")
    print(f"  interval : {binding.poll_interval_seconds}s")
    print(f"  enabled  : {binding.is_enabled}")
    print(f"  id       : {binding.binding_id}")
    return 0


def _cmd_bindings_set_enabled(repo: WhatsAppFetchRelayRepository, identifier: str, enabled: bool) -> int:
    binding = _resolve_binding(repo, identifier)
    if binding is None:
        print(f"No binding matching '{identifier}'.", file=sys.stderr)
        return 1
    repo.set_binding_enabled(binding.binding_id, enabled)
    print(f"{'Enabled' if enabled else 'Disabled'} binding for '{binding.group_name}'.")
    return 0


def _cmd_bindings_remove(repo: WhatsAppFetchRelayRepository, identifier: str) -> int:
    binding = _resolve_binding(repo, identifier)
    if binding is None:
        print(f"No binding matching '{identifier}'.", file=sys.stderr)
        return 1
    repo.delete_binding(binding.binding_id)
    print(f"Removed binding for '{binding.group_name}' (and its message history).")
    return 0


def _cmd_messages(repo: WhatsAppFetchRelayRepository, limit: int) -> int:
    messages = repo.get_recent_messages(limit)
    if not messages:
        print("No relayed messages yet.")
        return 0
    bindings = {b.binding_id: b.group_name for b in repo.get_bindings()}
    print(f"{'STATE':<10} {'GROUP':<20} {'FETCHED (UTC)':<20} MESSAGE")
    print("-" * 100)
    for m in messages:
        group = bindings.get(m.binding_id, "(deleted)")
        fetched = m.fetch_utc.strftime("%Y-%m-%d %H:%M:%S") if m.fetch_utc else ""
        print(f"{m.state.name:<10} {_clip(group, 20):<20} {fetched:<20} {_clip(m.message_text, 50)}")
    return 0


def _cmd_relay_status(repo: WhatsAppFetchRelayRepository, settings: SettingsRepository) -> int:
    bindings = repo.get_bindings()
    enabled_count = sum(1 for b in bindings if b.is_enabled)
    recent = repo.get_recent_messages(200)
    sent = sum(1 for m in recent if m.state.name == "SENT")
    print(f"Relay enabled : {_relay_enabled(settings)}")
    print(f"Bindings      : {len(bindings)} ({enabled_count} enabled)")
    print(f"Messages      : {len(recent)} recent, {sent} sent")
    if bindings:
        print("\nRun 'bindings list' for details.")
    return 0


def _cmd_relay_set_enabled(settings: SettingsRepository, enabled: bool) -> int:
    settings.set_value(SETTINGS_WHATSAPP_FETCH_RELAY_ENABLED, "true" if enabled else "false")
    print(f"Relay {'enabled' if enabled else 'disabled'} (persisted).")
    print("If the app is already running, restart it or wait for its next sync to pick this up.")
    return 0


def _cmd_chats(db_path: Path) -> int:
    if sys.platform != "win32":
        print("The 'chats' command needs a real Windows session with WhatsApp Desktop.", file=sys.stderr)
        return 2

    import asyncio

    from winspark.automation.sta_thread_manager import StaAutomationThreadManager
    from winspark.connectors.whatsapp import WhatsAppConnector

    async def _run() -> int:
        manager = StaAutomationThreadManager()
        try:
            connector = WhatsAppConnector(manager)
            handle = await connector.find_window_async()
            if handle is None:
                print("WhatsApp Desktop is not running.", file=sys.stderr)
                return 1
            rows = await connector.read_chat_rows_async(handle)
            if not rows:
                print("No chats visible in the sidebar.")
                return 0
            print(f"{'UNREAD':<7} CHAT")
            print("-" * 60)
            for row in rows:
                print(f"{row.unread_count:<7} {row.chat_name}")
            return 0
        finally:
            manager.dispose()

    return asyncio.run(_run())


def _clip(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="winspark.cli", description="Manage the winSpark Fetch-Webhook relay.")
    parser.add_argument("--db", type=Path, default=None, help="Path to the SQLite database (defaults to the app's).")
    sub = parser.add_subparsers(dest="command", required=True)

    bindings = sub.add_parser("bindings", help="Manage WhatsApp <-> webhook bindings.")
    bsub = bindings.add_subparsers(dest="bindings_command", required=True)
    bsub.add_parser("list", help="List all bindings.")
    add = bsub.add_parser("add", help="Add or update a binding.")
    add.add_argument("group", help="WhatsApp chat/group name (exactly as it appears in the sidebar).")
    add.add_argument("url", nargs="?", default="", help="Webhook GET URL (blank -> local mock URL for this group).")
    add.add_argument("--interval", type=int, default=FetchWebhookDefaults.DEFAULT_POLL_INTERVAL_SECONDS, help="Poll interval, seconds.")
    add.add_argument("--api-key", default="", help="Optional Bearer token sent with each poll.")
    add.add_argument("--disabled", action="store_true", help="Create the binding disabled.")
    for name, help_text in (("enable", "Enable a binding."), ("disable", "Disable a binding."), ("remove", "Delete a binding.")):
        p = bsub.add_parser(name, help=help_text)
        p.add_argument("binding", help="BindingId or group name.")

    messages = sub.add_parser("messages", help="Show recently relayed messages.")
    messages.add_argument("--limit", type=int, default=20, help="How many to show.")

    relay = sub.add_parser("relay", help="Inspect or toggle the relay.")
    rsub = relay.add_subparsers(dest="relay_command", required=True)
    rsub.add_parser("status", help="Show relay status.")
    rsub.add_parser("enable", help="Enable the relay (persisted).")
    rsub.add_parser("disable", help="Disable the relay (persisted).")

    sub.add_parser("chats", help="List live WhatsApp chats (Windows + running WhatsApp only).")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    db_path = args.db if args.db is not None else default_database_path()
    factory = ConnectionFactory(db_path)
    factory.initialize_schema()
    repo = WhatsAppFetchRelayRepository(factory)
    settings = SettingsRepository(factory)

    if args.command == "bindings":
        if args.bindings_command == "list":
            return _cmd_bindings_list(repo)
        if args.bindings_command == "add":
            return _cmd_bindings_add(repo, args)
        if args.bindings_command == "enable":
            return _cmd_bindings_set_enabled(repo, args.binding, True)
        if args.bindings_command == "disable":
            return _cmd_bindings_set_enabled(repo, args.binding, False)
        if args.bindings_command == "remove":
            return _cmd_bindings_remove(repo, args.binding)
    elif args.command == "messages":
        return _cmd_messages(repo, args.limit)
    elif args.command == "relay":
        if args.relay_command == "status":
            return _cmd_relay_status(repo, settings)
        if args.relay_command == "enable":
            return _cmd_relay_set_enabled(settings, True)
        if args.relay_command == "disable":
            return _cmd_relay_set_enabled(settings, False)
    elif args.command == "chats":
        return _cmd_chats(db_path)

    return 2


if __name__ == "__main__":
    sys.exit(main())
