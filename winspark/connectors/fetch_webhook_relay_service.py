"""Port of WinSpark.Infrastructure.Services.WhatsApp.WhatsAppFetchRelayService.

The orchestrator: poll each enabled binding's URL on its own schedule, parse
the response, dedupe against what's already been sent (by external id or
content hash), persist it, and hand it to the group sender — with retry on
failure up to MaxSendAttempts.

group_sender is any object exposing an async `send_to_group_async(group_name,
message_text) -> WhatsAppGroupSendResult` — in production that's
WhatsAppGroupSender (winspark/connectors/whatsapp_group_sender.py); tests use
a stub instead, since exercising a real send would actually deliver a message
to a real contact (see PORT_NOTES.md).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from winspark.connectors import fetch_webhook_client, openai_client, retrieval, trigger_match
from winspark.connectors.fetch_webhook_models import (
    FetchWebhookDefaults,
    WhatsAppFetchApiResult,
    WhatsAppFetchBindingEntity,
    WhatsAppFetchRelayMessageEntity,
    WhatsAppFetchRelayMessageState,
    WhatsAppGroupSendResult,
)
from winspark.connectors.fetch_webhook_mock_server import WhatsAppFetchLocalMockServer
from winspark.connectors.fetch_webhook_repository import WhatsAppFetchRelayRepository, compute_content_hash
from winspark.connectors.fetch_webhook_scheduler import FetchWebhookBindingScheduler
from winspark.connectors.fetch_webhook_url import normalize_poll_url
from winspark.constants import AI_COMMAND_PREFIX
from winspark.data.repositories import LogRepository
from winspark.domain.entities import LogEntity

logger = logging.getLogger(__name__)

_CITATION_RE = re.compile(r"\s*\(\[[^\]]+\]\([^)]*\)\)")   # ([site.com](https://...))
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")           # [text](url) -> text


# Words that suggest the message is about CURRENT information — worth the
# pricier web-search model. Everything else answers on the configured model,
# with an upgrade retry if that model admits its knowledge is stale.
_FRESH_INFO_RE = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|latest|current|currently|now|news|"
    r"recent|recently|this (week|month|year)|next (week|month|year|match|game|election)|"
    r"price|prices|stock|score|scores|weather|forecast|schedule|fixtures|results?|"
    r"when is|when's|what's happening|updates?|releases?|20[2-9][0-9])\b",
    re.IGNORECASE,
)
_STALE_KNOWLEDGE_RE = re.compile(
    r"knowledge cutoff|training data|as of my last (update|training)|"
    r"my (data|information|knowledge) (is|goes|only goes) up to|"
    r"i (do not|don't) have (access to )?(real.?time|current|up.to.date)",
    re.IGNORECASE,
)


def _needs_fresh_info(text: str) -> bool:
    return bool(_FRESH_INFO_RE.search(text or ""))


def _sounds_stale(reply_text: str) -> bool:
    return bool(_STALE_KNOWLEDGE_RE.search(reply_text or ""))


# How AI replies should read: a person texting, not an assistant answering.
# Appended to the chat's own prompt (which stays the persona/instructions).
_TEXTING_STYLE = (
    "You are texting inside a WhatsApp chat on the account owner's behalf. Reply like a person, "
    "not an assistant: short and natural (one or two sentences unless more is truly needed), in "
    "the same language and tone the chat is using. Use the conversation so far for context — "
    "don't re-ask things already said, don't repeat yourself, don't greet mid-conversation. "
    "No sign-offs, no 'as an AI' disclaimers, no bullet lists. At most one emoji, only if it fits."
)


def _is_settled(state) -> bool:
    """Is this reply DONE WITH — sent, or given up on?

    SENT alone used to mean "answered", which made a permanently failed send
    immortal: the row goes FAILED after MAX_SEND_ATTEMPTS, "answered" said no,
    so every poll three seconds later re-picked the same message, paid for a
    fresh AI reply, and re-entered the send path — for ever. One broken send
    became an endless rewrite loop that also burned an API call per tick.

    Treating FAILED as settled stops the retrying. It does NOT hide the
    failure: the activity feed already records `send_failed` with the reason,
    and the binding's status shows it."""
    return state in (
        WhatsAppFetchRelayMessageState.SENT,
        WhatsAppFetchRelayMessageState.FAILED,
    )


def _current_datetime_line() -> str:
    """A reference line giving the AI the real LOCAL date and time, so it can
    answer "what's the date/time" accurately instead of guessing (it had no
    clock before and once said "afternoon" late at night). Local, not UTC — the
    chat's timestamps are local. Hour is un-padded ("9:07 PM", not "09:07 PM")
    without relying on the non-portable %-I/%#I strftime flags."""
    now = datetime.now()
    clock = now.strftime("%I:%M %p").lstrip("0")
    return f"For reference, the current local date and time is {now.strftime('%A, %d %B %Y')}, {clock}."


def _render_memory(memory: list[tuple[str, str, str]]) -> str:
    """The chat's remembered messages as transcript lines the model can read."""
    lines = []
    for role, sender, text in memory:
        who = "You" if role == "me" else (sender or "Them")
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


def _strip_web_citations(text: str) -> str:
    """Web-search models decorate replies with markdown citations — fine in a
    browser, noise in a WhatsApp bubble (seen live: '([fifa.com](https://...))'
    mid-sentence and stray '#' heading marks). Keep the words, drop the markup."""
    cleaned = _CITATION_RE.sub("", text)
    cleaned = _MD_LINK_RE.sub('\\1', cleaned)
    cleaned = re.sub(r"^#+\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"[ 	]{2,}", " ", cleaned)
    return cleaned.strip().rstrip("#").strip()


class GroupSender(Protocol):
    async def send_to_group_async(self, group_name: str, message_text: str) -> WhatsAppGroupSendResult: ...


class WhatsAppFetchRelayService:
    def __init__(
        self,
        repository: WhatsAppFetchRelayRepository,
        log_repository: LogRepository,
        group_sender: GroupSender,
        local_mock: WhatsAppFetchLocalMockServer,
        scheduler: FetchWebhookBindingScheduler,
        openai_config_provider: Optional[Callable[[], tuple[str, str]]] = None,
        chat_memory=None,
    ) -> None:
        self._repository = repository
        self._log_repository = log_repository
        self._group_sender = group_sender
        self._local_mock = local_mock
        self._scheduler = scheduler
        # Where per-chat conversation memory lives. Defaults to the SQLite
        # repository (which implements the same append/get/clear surface); the
        # host swaps in a MongoDB-backed store when one is configured.
        self._memory = chat_memory if chat_memory is not None else repository
        self._default_memory = repository
        # Returns the app-wide (api_key, model) for OpenAI-backed bindings, or
        # None when OpenAI isn't configured for this host (web-only relay).
        self._openai_config_provider = openai_config_provider
        self._relay_enabled = False
        self._last_tick_hint: Optional[str] = None
        self._status_changed_handlers: list[Callable[[], None]] = []
        self._activity_handlers: list[Callable[[str, str, str], None]] = []
        self._relay_cycle_lock = asyncio.Lock()
        self._retry_sweep_lock = asyncio.Lock()
        # For "trigger" bindings: the hash of the last incoming message we've
        # already evaluated per binding, so we don't re-run the (possibly paid)
        # match on the same unchanged message every poll.
        self._last_evaluated_incoming: dict[str, str] = {}
        # Process-lived cache of message-text -> embedding vector, so RAG embeds
        # each archived message at most once. Rebuilt lazily after a restart.
        self._embed_cache: dict[str, list[float]] = {}
        # binding_id -> the chat's canonical (display) name, resolved once. Chat
        # memory is keyed by THIS, not by whatever the user typed to bind (a
        # phone number, a slightly-off name), so a binding's memory lands under
        # the same key the live-view path uses — one unified store per chat.
        self._canonical_name: dict[str, str] = {}

        scheduler.set_binding_poll_requested_handler(self._on_binding_poll_requested)

    def set_chat_memory(self, store) -> None:
        """Swap the chat-memory store at runtime (e.g. the user just configured
        MongoDB in settings). None restores the default SQLite store."""
        self._memory = store if store is not None else self._default_memory

    @property
    def is_relay_enabled(self) -> bool:
        return self._relay_enabled

    @property
    def last_tick_hint(self) -> Optional[str]:
        return self._last_tick_hint

    def on_status_changed(self, handler: Callable[[], None]) -> None:
        self._status_changed_handlers.append(handler)

    def _notify_status_changed(self) -> None:
        for handler in self._status_changed_handlers:
            handler()

    def on_activity(self, handler: Callable[[str, str, str], None]) -> None:
        """Subscribe to plain, structured automation activity: handler(chat,
        kind, detail). `kind` is a stable token (checking / received / sending /
        sent / send_failed / source_error / retrying / automation_on|off) that
        the UI turns into user-facing wording — the engine stays neutral about
        phrasing. Additive: with no handlers this is a no-op."""
        self._activity_handlers.append(handler)

    def _record_activity(self, chat: str, kind: str, detail: str = "") -> None:
        for handler in self._activity_handlers:
            try:
                handler(chat, kind, detail)
            except Exception:  # noqa: BLE001 - a bad activity subscriber must not break the relay
                logger.warning("activity handler failed", exc_info=True)

    async def set_relay_enabled_async(self, enabled: bool) -> None:
        self._relay_enabled = enabled
        self._scheduler.set_relay_enabled(enabled)
        logger.info("Fetch-Webhook relay %s", "enabled" if enabled else "disabled")
        self._record_activity("", "automation_on" if enabled else "automation_off")

        if enabled:
            self._ensure_local_mock_for_bindings()
            await self.sync_scheduler_async()
            for binding in self._repository.get_bindings():
                if binding.is_enabled:
                    await self.poll_binding_async(binding.binding_id)
        else:
            self._scheduler.sync_bindings([])

        self._notify_status_changed()

    def get_bindings(self) -> list[WhatsAppFetchBindingEntity]:
        return self._repository.get_bindings()

    async def save_binding_async(self, binding: WhatsAppFetchBindingEntity) -> None:
        normalized_url = normalize_poll_url(binding.fetch_url, binding.group_name)
        if normalized_url != binding.fetch_url:
            binding = replace(binding, fetch_url=normalized_url)

        self._repository.upsert_binding(binding)
        await self.sync_scheduler_async()
        self._ensure_local_mock_for_bindings()
        self._notify_status_changed()

    async def delete_binding_async(self, binding_id: str) -> None:
        self._scheduler.stop_binding(binding_id)
        # Chat memory is PERSISTENT — it deliberately survives deleting the
        # automation, so re-adding it later resumes with context intact. Only
        # the binding itself is removed here.
        self._repository.delete_binding(binding_id)
        await self.sync_scheduler_async()
        self._notify_status_changed()

    async def pause_binding_async(self, binding_id: str) -> None:
        self._repository.set_binding_enabled(binding_id, False)
        self._repository.update_binding_status(binding_id, "paused")
        await self.sync_scheduler_async()
        self._notify_status_changed()

    async def resume_binding_async(self, binding_id: str) -> None:
        self._repository.set_binding_enabled(binding_id, True)
        self._repository.update_binding_status(binding_id, "blank")
        await self.sync_scheduler_async()

        binding = self._repository.get_binding(binding_id)
        if binding is not None and self._relay_enabled:
            self._scheduler.start_or_update(binding.binding_id, binding.poll_interval_seconds)
            await self._process_retries_for_binding(binding)
            await self.poll_binding_async(binding_id)
            self._last_tick_hint = f"Resumed {binding.group_name.strip()} — polling active."

        self._notify_status_changed()

    async def poll_binding_now_async(self, binding_id: str) -> None:
        await self.poll_binding_async(binding_id, manual=True)

    def get_recent_messages(self, limit: int = 50) -> list[WhatsAppFetchRelayMessageEntity]:
        return self._repository.get_recent_messages(limit)

    async def inject_test_message_async(self, group_name: str, message_text: str) -> None:
        self._ensure_local_mock_for_bindings()
        lines = [line.strip() for line in message_text.replace("\r", "\n").split("\n") if line.strip()]
        if not lines:
            return

        for line in lines:
            self._local_mock.inject_message(group_name, line)

        queued = self._local_mock.get_queued_count(group_name)
        self._last_tick_hint = (
            f"Queued 1 message for {group_name.strip()} ({queued} pending) — next poll will relay it."
            if len(lines) == 1
            else f"Queued {len(lines)} messages for {group_name.strip()} ({queued} pending) — one per poll."
        )
        self._notify_status_changed()

    async def sync_scheduler_async(self) -> None:
        bindings = self._repository.get_bindings()
        self._scheduler.sync_bindings(bindings if self._relay_enabled else [])
        self._ensure_local_mock_for_bindings()

    async def trigger_poll_now_async(self) -> None:
        for binding in self._repository.get_bindings():
            if binding.is_enabled:
                await self.poll_binding_async(binding.binding_id)

    async def process_tick_async(self) -> None:
        await self._process_retries()

    async def poll_binding_async(self, binding_id: str, manual: bool = False) -> None:
        if not self._relay_enabled and not manual:
            return

        async with self._relay_cycle_lock:
            binding = self._repository.get_binding(binding_id)
            if binding is None or not binding.is_enabled:
                return

            try:
                self._ensure_local_mock_for_bindings()
                await self._process_retries_for_binding(binding)
                await self._poll_binding_fetch(binding)
            except Exception:  # noqa: BLE001
                logger.warning("Fetch-Webhook poll failed for binding %s", binding_id, exc_info=True)

    async def _on_binding_poll_requested(self, binding_id: str) -> None:
        await self.poll_binding_async(binding_id)

    async def _process_retries(self) -> None:
        if not self._relay_enabled or self._retry_sweep_lock.locked():
            return

        async with self._retry_sweep_lock:
            for message in self._repository.get_retryable_messages():
                binding = self._repository.get_binding(message.binding_id)
                if binding is not None and binding.is_enabled:
                    await self._send_stored_message(binding, message)

    async def _process_retries_for_binding(self, binding: WhatsAppFetchBindingEntity) -> None:
        for message in self._repository.get_retryable_messages():
            if message.binding_id == binding.binding_id:
                await self._send_stored_message(binding, message)

    async def _poll_binding_fetch(self, binding: WhatsAppFetchBindingEntity) -> None:
        if binding.reply_source == "web":
            binding = self._ensure_binding_poll_url(binding)
        self._repository.increment_binding_poll_count(binding.binding_id)
        self._record_activity(binding.group_name, "checking")

        fetch = await self._fetch_reply(binding)
        now = datetime.now(timezone.utc)

        if fetch.is_error:
            self._repository.update_binding_status(binding.binding_id, "error", last_fetch_utc=now, last_error=fetch.error_message or "Fetch error")
            self._record_activity(binding.group_name, "source_error", fetch.error_message or "")
            self._notify_status_changed()
            return

        if not fetch.has_message or not (fetch.message or "").strip():
            # If we're polling the local mock but messages are queued for this
            # group under a *different* slug, the poll returns empty while the
            # queue is non-empty — a strong hint the poll URL is wrong. Surface
            # that instead of a bare "no message". (Ported from upstream.)
            hint = ""
            if binding.reply_source == "web" and _is_localhost_poll_url(binding.fetch_url):
                queued = self._local_mock.get_queued_count(binding.group_name)
                if queued > 0:
                    hint = f"No message — {queued} queued for this chat but the poll URL may be wrong ({binding.fetch_url})"
            self._repository.update_binding_status(binding.binding_id, "blank", last_fetch_utc=now, last_error=hint)
            self._notify_status_changed()
            return

        content_hash = compute_content_hash(fetch.message)
        existing = self._repository.find_message(binding.binding_id, fetch.external_id, content_hash)

        if existing is not None:
            if existing.state == WhatsAppFetchRelayMessageState.FAILED:
                # Given up on already. Re-entering the send path here just
                # re-pasted the message into the compose box on every poll —
                # _send_stored_message bails on the attempt count, but only
                # after the text is back in the box. Stop before that.
                self._repository.update_binding_status(
                    binding.binding_id, "send-failed", last_fetch_utc=now,
                    last_error=existing.last_error or "Send failed",
                )
                self._notify_status_changed()
                return
            if existing.state in (
                WhatsAppFetchRelayMessageState.PENDING,
                WhatsAppFetchRelayMessageState.RETRYING,
                WhatsAppFetchRelayMessageState.SENDING,
            ):

                await self._send_stored_message(binding, replace(existing, message_text=fetch.message))
                return

            if existing.state == WhatsAppFetchRelayMessageState.SENT:
                if fetch.external_id and existing.external_id == fetch.external_id:
                    self._repository.update_binding_status(
                        binding.binding_id, "duplicate", last_fetch_utc=now, last_error=f"Already sent (external id {fetch.external_id})"
                    )
                    self._notify_status_changed()
                    return
                # same text fetched again without an external id to distinguish — re-deliver

        if self._repository.has_in_flight_message(binding.binding_id, fetch.external_id, content_hash):
            in_flight = self._repository.find_message(binding.binding_id, fetch.external_id, content_hash)
            if in_flight is not None and in_flight.state in (
                WhatsAppFetchRelayMessageState.PENDING,
                WhatsAppFetchRelayMessageState.SENDING,
            ):

                await self._send_stored_message(binding, replace(in_flight, message_text=fetch.message))
                return

            self._repository.update_binding_status(binding.binding_id, "duplicate", last_fetch_utc=now, last_error="Same message already being processed")
            self._notify_status_changed()
            return

        stored = WhatsAppFetchRelayMessageEntity(
            binding_id=binding.binding_id,
            external_id=fetch.external_id,
            message_text=fetch.message,
            content_hash=content_hash,
            state=WhatsAppFetchRelayMessageState.PENDING,
            fetch_utc=now,
            parse_strategy=fetch.parse_strategy,
        )
        self._repository.insert_message(stored)
        self._repository.update_binding_status(binding.binding_id, "message", last_fetch_utc=now, last_message_received_utc=now)

        logger.info("Fetch-Webhook stored message for %s via %s (%d chars)", binding.group_name, fetch.parse_strategy, len(fetch.message))
        self._record_activity(binding.group_name, "received", fetch.message)
        await self._send_stored_message(binding, stored)

    async def _fetch_reply(self, binding: WhatsAppFetchBindingEntity):
        """Produce the next message to relay for this chat, from whichever source
        the binding is configured for. Returns a WhatsAppFetchApiResult so the
        downstream dedupe/persist/send path is identical for every source."""
        if binding.reply_source == "openai":
            return await self._openai_reply(binding)
        if binding.reply_source == "trigger":
            return await self._trigger_reply(binding)
        return await fetch_webhook_client.fetch_async(binding.fetch_url, binding.api_key)

    async def _trigger_reply(self, binding: WhatsAppFetchBindingEntity):
        """Watch for an incoming message that matches the chat's trigger phrase
        and, when one arrives, answer with the chat's canned reply. Matching is
        semantic when an OpenAI key is set, else literal (word-based). The reply
        is deduped on the incoming message so it fires once per matching message."""
        read_incoming = getattr(self._group_sender, "read_last_incoming_message_async", None)
        if read_incoming is None:
            return WhatsAppFetchApiResult.failed("Reading incoming messages isn't available on this device.")
        if not binding.trigger_text.strip() or not binding.reply_text.strip():
            return WhatsAppFetchApiResult.blank("trigger")

        incoming_text = await read_incoming(binding.group_name)
        if not incoming_text or not incoming_text.strip():
            return WhatsAppFetchApiResult.blank("trigger")

        # Skip re-evaluating the same unchanged incoming message every poll.
        incoming_hash = compute_content_hash(incoming_text)
        if self._last_evaluated_incoming.get(binding.binding_id) == incoming_hash:
            return WhatsAppFetchApiResult.blank("trigger")
        self._last_evaluated_incoming[binding.binding_id] = incoming_hash

        if not await self._incoming_matches_trigger(binding.trigger_text, incoming_text):
            return WhatsAppFetchApiResult.blank("trigger")

        external_id = "trigger:" + incoming_hash
        return WhatsAppFetchApiResult.with_message(binding.reply_text.strip(), external_id=external_id, strategy="trigger")

    async def _incoming_matches_trigger(self, trigger_text: str, incoming_text: str) -> bool:
        api_key, model, base_url = self._openai_config()
        if api_key:
            verdict = await openai_client.classify_intent_match_async(
                api_key, model, trigger_text, incoming_text, base_url=base_url
            )
            if verdict is not None:
                return verdict
        return trigger_match.literal_match(trigger_text, incoming_text)

    async def _openai_reply(self, binding: WhatsAppFetchBindingEntity):
        """Ask the AI service for the next message. "generate" mode produces one
        from the chat's prompt on every check; "reply" mode reads the newest
        incoming message and answers it (once); "command" mode is "reply" but
        only for messages that address the bot by name ("!winspark …")."""
        api_key, model, base_url, fallback = self._openai_config_with_fallback()
        if not api_key:
            return WhatsAppFetchApiResult.failed("No AI key set — add it in the AI settings.")

        if binding.ai_mode == "command":
            command_word = (binding.trigger_text or "").strip().lstrip("!@/#").strip()
            if not command_word:
                return WhatsAppFetchApiResult.failed(
                    "No command word set — choose the word people will type after "
                    f"'{AI_COMMAND_PREFIX}' to call the assistant."
                )
            return await self._openai_reply_to_incoming(
                binding, api_key, model, base_url, fallback, command_word=command_word)

        if binding.ai_mode == "reply":
            return await self._openai_reply_to_incoming(binding, api_key, model, base_url, fallback)

        persona = (binding.ai_prompt or "").strip()
        gen_system = f"{persona}\n\n{_current_datetime_line()}" if persona else _current_datetime_line()
        result = await self._generate_with_fallback(api_key, model, base_url, fallback, gen_system, "")
        if not result.ok:
            return WhatsAppFetchApiResult.failed(result.error)
        # No external id: each generation is genuinely a new message to post.
        return WhatsAppFetchApiResult.with_message(result.text, external_id=None, strategy="openai-generate")

    async def _memory_key(self, binding: WhatsAppFetchBindingEntity) -> str:
        """The chat-memory key for a binding: the chat's canonical display name
        (resolved once from WhatsApp and cached), so memory for a chat bound by
        phone number — or by a slightly different name — unifies with the memory
        the live-view path stores under the chat's real name. Falls back to the
        typed `group_name` when the chat can't be resolved (e.g. WhatsApp closed)."""
        cached = self._canonical_name.get(binding.binding_id)
        if cached:
            return cached
        resolve = getattr(self._group_sender, "resolve_chat_row_async", None)
        if resolve is not None:
            try:
                _handle, row = await resolve(binding.group_name)
                name = (getattr(row, "chat_name", "") or "").strip() if row is not None else ""
                if name:
                    self._canonical_name[binding.binding_id] = name
                    return name
            except Exception:  # noqa: BLE001 - resolution is best-effort
                pass
        return binding.group_name

    async def _retrieve_relevant(self, binding: WhatsAppFetchBindingEntity, question: str, exclude: set,
                                 memory_key: Optional[str] = None):
        """The few OLDER archived messages most relevant to `question`, excluding
        anything in `exclude` (the recent window). Semantic (OpenAI embeddings)
        when a key is available, else lexical overlap — the hybrid RAG path.
        Returns (role, sender, text) tuples oldest-first, or [] when there's
        nothing useful. Never raises: retrieval is a best-effort enhancement."""
        get_rich = getattr(self._memory, "get_chat_memory_rich", None)
        if get_rich is None:
            return []
        try:
            # Search deep into the (on MongoDB, unbounded) history — not just the
            # local archive cap — so retrieval genuinely draws on the full chat.
            rows = get_rich(memory_key or binding.group_name, FetchWebhookDefaults.RAG_SEARCH_LIMIT)
        except Exception:  # noqa: BLE001
            return []
        candidates = [
            r for r in rows
            if (r.get("text") or "").strip() and (r.get("text") or "").strip() not in exclude
        ]
        if not candidates:
            return []
        texts = [r["text"] for r in candidates]

        picked: list[int] = []
        api_key, model, base_url = self._openai_config()
        if api_key:
            query_vecs = await openai_client.embed_texts_async(api_key, [question], base_url=base_url)
            cand_vecs = await self._embed_cached(api_key, base_url, texts)
            if query_vecs and cand_vecs is not None:
                picked = retrieval.rank_semantic(query_vecs[0], cand_vecs, k=FetchWebhookDefaults.RAG_TOP_K)
        if not picked:  # no key, embeddings failed (e.g. Groq), or nothing over the floor
            picked = retrieval.rank_lexical(question, texts, k=FetchWebhookDefaults.RAG_TOP_K)

        # Present oldest-first for a natural read, regardless of relevance order.
        return [
            (candidates[i].get("role", ""), candidates[i].get("sender", ""), candidates[i]["text"])
            for i in sorted(picked)
        ]

    async def _embed_cached(self, api_key: str, base_url: str, texts: list[str]):
        """Embeddings for `texts`, aligned by index, caching each text so a given
        archived message is embedded once per process. Returns None if embedding
        the uncached ones fails (the signal to fall back to lexical)."""
        # De-duplicate the uncached texts (a message repeated across the window
        # is embedded once), then embed in provider-sized batches.
        missing = list(dict.fromkeys(t for t in texts if t not in self._embed_cache))
        batch = FetchWebhookDefaults.EMBED_BATCH_SIZE
        for start in range(0, len(missing), batch):
            chunk = missing[start:start + batch]
            vectors = await openai_client.embed_texts_async(api_key, chunk, base_url=base_url)
            if vectors is None:
                return None  # any batch failing -> fall back to lexical for the whole set
            for text, vector in zip(chunk, vectors):
                self._embed_cache[text] = vector
        return [self._embed_cache[t] for t in texts]

    async def _pick_incoming_to_answer(self, binding: WhatsAppFetchBindingEntity):
        """Decide which incoming message to reply to this poll, returning
        (text, sender, identity) or (None, "", "").

        When the sender exposes the richer recent-messages read, answer the
        OLDEST incoming message we haven't answered yet — so two messages that
        arrive inside one poll interval (e.g. "Where is London" then "Where is
        punjab") are BOTH answered, in order, over successive polls, rather than
        the first being skipped because only the newest was ever read.

        It won't retroactively answer pre-existing history: until we've answered
        something in this chat, it only ever answers the single newest message
        (the original behaviour). The "already answered" record is the SENT reply
        rows in the repository, so this survives restarts. Falls back to
        newest-only when the richer read isn't available (older senders/stubs)."""
        read_recent = getattr(self._group_sender, "read_recent_incoming_async", None)
        if read_recent is not None:
            incoming = [m for m in await read_recent(binding.group_name) if (m.text or "").strip()]
            if not incoming:
                return None, "", ""
            identities = self._message_identities(incoming)
            # Highest index we've already replied to (SENT). -1 = we haven't
            # replied to anything visible yet → this is our baseline.
            answered_idx = -1
            texts_seen: set[str] = set()
            first_of_text: list[bool] = []
            for message in incoming:
                first_of_text.append(message.text not in texts_seen)
                texts_seen.add(message.text)
            for i, message in enumerate(incoming):
                if self._already_answered(
                        binding.binding_id, identities[i], message.text, first_of_text[i]):
                    answered_idx = i
            if answered_idx == -1:
                target, identity = incoming[-1], identities[-1]      # baseline: newest only
            elif answered_idx < len(incoming) - 1:
                target = incoming[answered_idx + 1]                  # oldest one not yet answered
                identity = identities[answered_idx + 1]
            else:
                return None, "", ""                     # nothing new since our last reply
            return target.text, (target.sender or ""), identity

        # Fallback: newest incoming message only.
        read_incoming_full = getattr(self._group_sender, "read_last_incoming_async", None)
        if read_incoming_full is not None:
            message = await read_incoming_full(binding.group_name)
            if message is None:
                return None, "", ""
            return message.text, (message.sender or ""), self._message_identities([message])[0]
        read_incoming = getattr(self._group_sender, "read_last_incoming_message_async", None)
        if read_incoming is not None:
            text = await read_incoming(binding.group_name)
            if not text:
                return None, "", ""
            # No message object here, so no timestamp to distinguish repeats —
            # this reader only ever exposes the newest text.
            return text, "", "reply:" + compute_content_hash(text)
        return None, "", ""

    @staticmethod
    def _message_identities(messages) -> list[str]:
        """A stable id per message that survives THE SAME THING BEING SAID TWICE.

        Identity used to be `hash(text)` alone, which quietly made a repeat
        unanswerable forever: ask "what's the weather" today and the identical
        message tomorrow hashes to the row we already answered, so it is skipped
        as a duplicate. People repeat themselves constantly — at a bot and at
        each other — so text alone cannot be the identity.

        Identity is therefore (bubble timestamp, how-many-times-this-exact-text-
        has-appeared-so-far, text). The occurrence count is what covers two
        identical messages inside the same minute, where the timestamps match
        too. It shifts when old messages scroll out of the read window, but only
        ever downward — an id then collides with an EARLIER occurrence that is
        already answered, so the failure direction is staying quiet rather than
        answering twice."""
        identities: list[str] = []
        seen: dict[tuple[str, str], int] = {}
        for message in messages:
            text = message.text or ""
            when = (getattr(message, "time_text", "") or "").strip().lower()
            key = (when, text)
            seen[key] = seen.get(key, 0) + 1
            identities.append("reply:" + compute_content_hash(f"{when}|{seen[key]}|{text}"))
        return identities

    def _already_answered(self, binding_id: str, identity: str,
                          text: str, first_of_its_text: bool) -> bool:
        prior = self._repository.find_message(binding_id, identity, "")
        if prior is not None:
            return _is_settled(prior.state)
        if first_of_its_text:
            # Rows written before identities included the timestamp used the
            # bare text hash. Honour them for the FIRST copy of a text only —
            # applying it to later copies would reinstate the very bug this
            # replaces — so upgrading doesn't re-answer the visible backlog.
            legacy = self._repository.find_message(binding_id, "reply:" + compute_content_hash(text), "")
            if legacy is not None:
                return _is_settled(legacy.state)
        return False

    async def _pick_command_to_answer(self, binding: WhatsAppFetchBindingEntity, command_word: str):
        """Like `_pick_incoming_to_answer`, but only messages that ADDRESS the
        bot ("!winspark what's the weather") are candidates. Returns
        (text, sender, query) or (None, "", "").

        This cannot reuse `_pick_incoming_to_answer` and filter afterwards, and
        the reason is a deadlock: that method returns the oldest message with no
        SENT reply row. A message that isn't a command never gets such a row —
        we're not supposed to answer it — so it would be returned again on every
        poll forever, and any real command sitting behind it in the backlog would
        never be reached. Filtering to commands FIRST means only messages that
        can be answered are ever candidates.

        Keeps the same don't-backfill-history rule as the plain reply path: until
        we've answered a command in this chat, only the newest one counts, so
        switching this on doesn't fire off replies to every old command still
        visible in the conversation."""
        read_recent = getattr(self._group_sender, "read_recent_incoming_async", None)
        if read_recent is not None:
            incoming = [m for m in await read_recent(binding.group_name) if (m.text or "").strip()]
            # Identities are numbered across ALL incoming messages, not just the
            # matching ones, so they don't renumber as chatter comes and goes.
            identities = self._message_identities(incoming)

            commands = []
            texts_seen: set[str] = set()
            for message, identity in zip(incoming, identities):
                text = (message.text or "").strip()
                matched, query = trigger_match.command_match(command_word, text)
                if matched:
                    commands.append((message, query, identity, text not in texts_seen))
                texts_seen.add(text)
            if not commands:
                return None, "", "", ""

            answered_idx = -1
            for i, (message, _query, identity, first_of_text) in enumerate(commands):
                if self._already_answered(
                        binding.binding_id, identity, message.text, first_of_text):
                    answered_idx = i
            if answered_idx == -1:
                target, query, identity, _first = commands[-1]   # newest only; don't backfill
            elif answered_idx < len(commands) - 1:
                target, query, identity, _first = commands[answered_idx + 1]
            else:
                return None, "", "", ""                          # every visible command answered
            return target.text, (target.sender or ""), query, identity

        # Fallback for senders without the richer read: newest incoming only.
        text, sender, identity = await self._pick_incoming_to_answer(binding)
        if not text:
            return None, "", "", ""
        matched, query = trigger_match.command_match(command_word, text)
        if not matched:
            return None, "", "", ""
        return text, sender, query, identity

    async def _openai_reply_to_incoming(
        self, binding: WhatsAppFetchBindingEntity, api_key: str, model: str, base_url: str,
        fallback: str = "", command_word: str = "",
    ):
        """Read the newest incoming message and, if it's one we haven't answered,
        ask the AI service for a reply. The external id is derived from the
        incoming message so the dedupe pipeline guarantees we reply to it exactly
        once — and once we've replied, our own outgoing message becomes newest,
        so the next check reads nothing new.

        With `command_word` set, only messages that address the bot by name
        ("!winspark what's the weather") are answered, and the AI is asked the
        QUERY with the addressing stripped off. Everything downstream — dedupe,
        memory, retrieval, sending — is deliberately identical either way."""
        has_reader = any(
            getattr(self._group_sender, name, None) is not None
            for name in ("read_recent_incoming_async", "read_last_incoming_async", "read_last_incoming_message_async")
        )
        if not has_reader:
            return WhatsAppFetchApiResult.failed("Reading incoming messages isn't available on this device.")

        strategy = "openai-command" if command_word else "openai-reply"
        if command_word:
            incoming_text, sender, question, external_id = await self._pick_command_to_answer(
                binding, command_word)
        else:
            incoming_text, sender, external_id = await self._pick_incoming_to_answer(binding)
            question = incoming_text
        if not incoming_text or not incoming_text.strip():
            return WhatsAppFetchApiResult.blank(strategy)

        # Already dealt with? Don't pay for a regeneration. "Dealt with" covers
        # a send we GAVE UP on as well as one that succeeded — otherwise a
        # permanently failing send bought a fresh AI reply on every poll.
        prior = self._repository.find_message(binding.binding_id, external_id, "")
        if prior is not None and _is_settled(prior.state):
            return WhatsAppFetchApiResult.blank(strategy)

        # Per-chat memory. The RECENT window is always included so replies
        # follow the immediate thread; RAG then pulls in a FEW older messages
        # relevant to this question (from the larger archive) — so the AI can
        # answer "what did we say about X" without us ever sending the whole
        # history. The newest incoming is passed separately below, so drop it if
        # it's already the last remembered line (a retry after a failed send).
        recent = FetchWebhookDefaults.CHAT_MEMORY_MESSAGES
        archive_keep = FetchWebhookDefaults.CHAT_MEMORY_ARCHIVE
        mem_key = await self._memory_key(binding)
        memory = self._memory.get_chat_memory(mem_key, recent)
        # Is this message already the newest thing remembered? The live-view
        # reader records the same conversation independently, so without this
        # check both writers store it and the chat's memory grows duplicates —
        # seen live, the same question filed three times. Computed BEFORE the
        # trim below, which removes it from the PROMPT for a different reason.
        already_remembered = bool(memory) and memory[-1][0] == "them" \
            and memory[-1][2] == incoming_text.strip()
        if already_remembered:
            memory = memory[:-1]
        if prior is None and not already_remembered:
            self._memory.append_chat_memory(mem_key, "them", sender, incoming_text, keep=archive_keep)

        # What the AI is actually asked. In command mode that's the question with
        # the addressing stripped ("!winspark what's the weather" -> "what's the
        # weather"), so neither the prompt nor the retrieval below is polluted by
        # the trigger word. A bare "!winspark" leaves nothing, so fall back to the
        # raw text and let the system prompt handle the greeting.
        asked = (question or "").strip() or incoming_text.strip()

        # In a group, tell the AI WHO it's answering — but hash on the raw text
        # so already-answered messages stay answered across this change.
        ai_input = f"{sender}: {asked}" if sender and sender != "You" else asked

        # RAG: relevant OLDER messages, excluding whatever is already in the
        # recent window (no point retrieving what we're sending anyway).
        exclude = {t.strip() for _r, _s, t in memory}
        exclude.add(incoming_text.strip())
        relevant = await self._retrieve_relevant(binding, asked, exclude, memory_key=mem_key)

        transcript = _render_memory(memory)
        persona = (binding.ai_prompt or "").strip()
        style = f"{_TEXTING_STYLE}\n\n{_current_datetime_line()}"
        system = f"{persona}\n\n{style}" if persona else style
        if command_word:
            # Without this the AI keeps answering AS the account owner, because
            # that's what the plain reply mode's style prompt tells it to do.
            # Here it was addressed by name, so it's the one being asked.
            system = (
                f"You are an assistant called \"{command_word}\" in a chat. Someone addressed you "
                f"directly by writing \"{AI_COMMAND_PREFIX}{command_word}\" followed by what they "
                f"want. Answer that question directly and helpfully. If they only said your name "
                f"with no question, greet them briefly and ask what they need. Never mention the "
                f"\"{AI_COMMAND_PREFIX}{command_word}\" prefix in your reply.\n\n{system}"
            )
        sections: list[str] = []
        if relevant:
            sections.append("Relevant earlier messages (older context that may help answer):\n" + _render_memory(relevant))
        if transcript:
            sections.append(f"Recent conversation (oldest first):\n{transcript}")
        heading = "You were addressed directly — answer this" if command_word else "Newest message — answer this one"
        sections.append(f"{heading}:\n{ai_input}")
        user = "\n\n".join(sections) if (relevant or transcript) else ai_input
        result = await self._generate_with_fallback(
            api_key, model, base_url, fallback, system, user, route_text=asked
        )
        if not result.ok:
            return WhatsAppFetchApiResult.failed(result.error)

        return WhatsAppFetchApiResult.with_message(result.text, external_id=external_id, strategy=strategy)

    def _openai_config(self) -> tuple[str, str, str]:
        api_key, model, base_url, _fallback = self._openai_config_with_fallback()
        return api_key, model, base_url

    def _openai_config_with_fallback(self) -> tuple[str, str, str, str]:
        """(api_key, model, base_url, fallback_model) for the configured AI
        service. `fallback_model` is non-empty when `model` is a web-search
        model — if that fails, the reply retries on the fallback so an outage
        of the search model never silences an automation. Tolerates providers
        returning 2-4 items so older callers/tests keep working."""
        default_base = openai_client._DEFAULT_BASE_URL
        if self._openai_config_provider is None:
            return "", "", default_base, ""
        try:
            cfg = tuple(self._openai_config_provider())
        except Exception:  # noqa: BLE001
            logger.warning("AI config provider failed", exc_info=True)
            return "", "", default_base, ""
        api_key = (cfg[0] if len(cfg) > 0 else "") or ""
        model = (cfg[1] if len(cfg) > 1 else "") or ""
        base_url = (cfg[2] if len(cfg) > 2 else default_base) or default_base
        fallback = (cfg[3] if len(cfg) > 3 else "") or ""
        return api_key.strip(), model.strip(), base_url, fallback.strip()

    async def _generate_with_fallback(self, api_key: str, model: str, base_url: str,
                                      fallback: str, system: str, user: str,
                                      route_text: str = ""):
        """`model` is the web-search model when web lookup is on; `fallback` is
        the user's configured model. Smart routing keeps costs down: only
        messages that look like they need CURRENT information go to the search
        model. Everything else runs on the configured model first — and if that
        reply admits its knowledge is stale ("as of my last update..."), it's
        retried once on the search model. Either direction, a failure falls
        back to the other model so an outage never silences an automation.
        `route_text` narrows the freshness check to just the newest message
        when `user` also carries conversation history."""
        web_mode = bool(fallback) and fallback != model
        if web_mode and not _needs_fresh_info(route_text or user):
            result = await openai_client.generate_reply_async(api_key, fallback, system, user, base_url=base_url)
            if result.ok and not _sounds_stale(result.text):
                return replace(result, text=_strip_web_citations(result.text))
            upgraded = await openai_client.generate_reply_async(api_key, model, system, user, base_url=base_url)
            if upgraded.ok:
                return replace(upgraded, text=_strip_web_citations(upgraded.text))
            return replace(result, text=_strip_web_citations(result.text)) if result.ok else upgraded

        result = await openai_client.generate_reply_async(api_key, model, system, user, base_url=base_url)
        if not result.ok and fallback and fallback != model:
            logger.warning("model %s failed (%s) — retrying with %s", model, result.error[:80], fallback)
            result = await openai_client.generate_reply_async(api_key, fallback, system, user, base_url=base_url)
        if result.ok:
            result = replace(result, text=_strip_web_citations(result.text))
        return result

    # ------------------------------------------------------------------
    async def _send_stored_message(self, binding: WhatsAppFetchBindingEntity, message: WhatsAppFetchRelayMessageEntity) -> None:

        current = self._repository.get_message_by_id(message.message_id)
        if current is None:
            return
        if current.state in (WhatsAppFetchRelayMessageState.SENT, WhatsAppFetchRelayMessageState.SENDING):
            return
        if current.attempt_count >= FetchWebhookDefaults.MAX_SEND_ATTEMPTS:
            return

        message_text = message.message_text.strip() or current.message_text
        message = replace(current, message_text=message_text)

        attempt = message.attempt_count + 1
        sending = replace(message, state=WhatsAppFetchRelayMessageState.SENDING, attempt_count=attempt, next_retry_utc=None)
        self._repository.update_message(sending)
        self._repository.update_binding_status(binding.binding_id, "sending")
        self._record_activity(binding.group_name, "sending", message.message_text)
        self._notify_status_changed()

        result = await self._group_sender.send_to_group_async(binding.group_name, message.message_text)

        if result.success:
            sent_utc = datetime.now(timezone.utc)
            self._repository.update_message(replace(sending, state=WhatsAppFetchRelayMessageState.SENT, sent_utc=sent_utc, last_error=""))
            self._repository.increment_binding_sent_count(binding.binding_id, sent_utc)
            # Whatever the source (AI, web post, trigger), what we sent is part
            # of the chat — remember it (under the canonical key) so later AI
            # replies follow the thread. Skipped when it's already the newest
            # thing remembered: the live-view reader records the same outgoing
            # message when it sees it on screen, and two writers with no shared
            # guard is what filled memory with triplicates.
            memory_key = await self._memory_key(binding)
            recent = self._memory.get_chat_memory(memory_key, 1)
            if not (recent and recent[-1][0] == "me"
                    and recent[-1][2] == message.message_text.strip()):
                self._memory.append_chat_memory(
                    memory_key, "me", "", message.message_text,
                    keep=FetchWebhookDefaults.CHAT_MEMORY_ARCHIVE,
                )
            state = "sent-verified" if result.verified else "sent-unverified"
            self._repository.update_binding_status(binding.binding_id, state, last_send_utc=sent_utc)
            self._record_activity(binding.group_name, "sent", message.message_text)

            self._log_repository.insert(
                LogEntity(
                    level="Information",
                    source="WhatsAppFetchRelayService",
                    message=f"Fetch-Webhook sent to {binding.group_name}: {_truncate(message.message_text, 120)}",
                    timestamp_utc=sent_utc,
                )
            )
            logger.info("Fetch-Webhook sent message to %s", binding.group_name)
            self._notify_status_changed()
            return

        reason = result.failure_reason
        logger.warning("Fetch-Webhook send failed for %s (attempt %d): %s", binding.group_name, attempt, reason)

        if attempt >= FetchWebhookDefaults.MAX_SEND_ATTEMPTS:
            self._repository.update_message(replace(sending, state=WhatsAppFetchRelayMessageState.FAILED, last_error=reason))
            self._repository.update_binding_status(binding.binding_id, "send-failed", last_error=reason)
            self._record_activity(binding.group_name, "send_failed", reason)
        else:
            next_retry = datetime.now(timezone.utc).timestamp() + FetchWebhookDefaults.RETRY_DELAY_SECONDS
            next_retry_dt = datetime.fromtimestamp(next_retry, tz=timezone.utc)
            self._repository.update_message(
                replace(sending, state=WhatsAppFetchRelayMessageState.RETRYING, last_error=reason, next_retry_utc=next_retry_dt)
            )
            self._repository.update_binding_status(binding.binding_id, "retrying", last_error=reason)
            self._record_activity(binding.group_name, "retrying", reason)

        self._notify_status_changed()

    def _ensure_local_mock_for_bindings(self) -> None:
        self._local_mock.ensure_started(FetchWebhookDefaults.MOCK_PORT)
        groups = [b.group_name.strip() for b in self._repository.get_bindings() if b.is_enabled and b.group_name.strip()]
        self._local_mock.configure_round_robin_groups(groups)

    def _ensure_binding_poll_url(self, binding: WhatsAppFetchBindingEntity) -> WhatsAppFetchBindingEntity:
        normalized = normalize_poll_url(binding.fetch_url, binding.group_name)
        if normalized == binding.fetch_url:
            return binding


        fixed = replace(binding, fetch_url=normalized)
        self._repository.upsert_binding(fixed)
        return fixed


def _truncate(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else value[:max_len] + "…"


def _is_localhost_poll_url(url: Optional[str]) -> bool:
    if not url or not url.strip():
        return False
    from urllib.parse import urlparse

    host = (urlparse(url.strip()).hostname or "").lower()
    return host in ("localhost", "127.0.0.1")
