"""Port of WinSpark.Infrastructure.Services.WhatsApp.WhatsAppFetchLocalMockServer.

A local GET API at localhost:5001/webhook/{group} (destructive read — dequeues
one message) with POST endpoints to inject test messages, matching the
README's documented local-testing workflow ("expand Test with local mock API
and queue a message to http://localhost:5001/webhook/{ChatName}").

Endpoints ported: GET/POST /webhook/{group}, POST /api/inject, POST
/api/inject/{group}, GET /api/queue/{group}, GET /api/status. The batch-inject
variants from the .NET version aren't ported (lower-value, not part of the
documented core workflow).
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)


class WhatsAppFetchLocalMockServer:
    def __init__(self) -> None:
        self._queues: dict[str, deque[str]] = {}
        self._gate = threading.Lock()
        self._round_robin_gate = threading.Lock()
        self._round_robin_groups: list[str] = []
        self._round_robin_index = 0
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._active_port = 0

    @property
    def is_listening(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        return self._active_port

    def ensure_started(self, port: int) -> None:
        with self._gate:
            if self.is_listening and self._active_port == port:
                return

            self._stop_core()
            self._active_port = port
            try:
                mock_server = self

                class _Handler(BaseHTTPRequestHandler):
                    server_version = "WinSparkFetchWebhookMock/1.0"

                    def log_message(self, fmt, *args):  # noqa: A002
                        logger.debug("mock server: " + fmt, *args)

                    def do_GET(self):  # noqa: N802
                        mock_server._handle_request(self, "GET")

                    def do_POST(self):  # noqa: N802
                        mock_server._handle_request(self, "POST")

                self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
                self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
                self._thread.start()
                logger.info("Fetch-Webhook mock API listening on http://localhost:%d/", port)
            except OSError as ex:
                logger.error("Fetch-Webhook mock API could not start on port %d: %s", port, ex)
                self._httpd = None

    def get_queued_count(self, group_name: str) -> int:
        return len(self._queues.get(_normalize_group(group_name), ()))

    def total_queued_count(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def inject_message(self, group_name: str, message: str) -> None:
        trimmed = message.strip()
        if not trimmed or not group_name or not group_name.strip():
            return

        key = _normalize_group(group_name)
        queue = self._queues.setdefault(key, deque())
        payload = trimmed if trimmed.startswith("{") else json.dumps({"message": trimmed})
        queue.append(payload)

    def configure_round_robin_groups(self, group_names: list[str]) -> None:
        ordered = list(dict.fromkeys(g.strip() for g in group_names if g and g.strip()))
        with self._round_robin_gate:
            self._round_robin_groups = ordered
            if self._round_robin_index >= len(self._round_robin_groups):
                self._round_robin_index = 0

    def get_round_robin_groups(self) -> list[str]:
        with self._round_robin_gate:
            return list(self._round_robin_groups)

    def inject_message_round_robin(self, message: str) -> Optional[str]:
        trimmed = message.strip()
        if not trimmed:
            return None

        with self._round_robin_gate:
            if not self._round_robin_groups:
                return None
            target_group = self._round_robin_groups[self._round_robin_index]
            self._round_robin_index = (self._round_robin_index + 1) % len(self._round_robin_groups)

        self.inject_message(target_group, trimmed)
        return target_group

    def has_pending_message(self, group_name: str) -> bool:
        return bool(self._queues.get(_normalize_group(group_name)))

    def stop(self) -> None:
        with self._gate:
            self._stop_core()

    def _stop_core(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        self._httpd = None
        self._thread = None

    def _dequeue_for_group(self, group_name: str) -> str:
        queue = self._queues.get(_normalize_group(group_name))
        if queue:
            return queue.popleft()
        return ""

    def _handle_request(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = handler.path.split("?", 1)[0].strip("/")
        body = ""
        code = 200

        try:
            if path == "api/status":
                queued_groups = {k: len(v) for k, v in self._queues.items() if v}
                body = json.dumps(
                    {
                        "port": self._active_port,
                        "sharedInject": True,
                        "roundRobinOrder": self.get_round_robin_groups(),
                        "queued": self.total_queued_count(),
                        "groups": queued_groups,
                    }
                )
            elif path == "api/inject":
                if method != "POST":
                    body, code = json.dumps({"error": "use POST"}), 405
                else:
                    posted = _read_body(handler)
                    if not posted.strip():
                        body, code = json.dumps({"error": "empty body"}), 400
                    else:
                        target = self.inject_message_round_robin(posted)
                        if target is None:
                            body, code = json.dumps({"error": "no enabled group bindings — add bindings in WinSpark first"}), 400
                        else:
                            body = json.dumps({"ok": True, "mode": "round-robin", "added": 1, "group": target})
            elif path.startswith("api/inject/"):
                group_name = unquote(path[len("api/inject/"):])
                if method != "POST":
                    body, code = json.dumps({"error": "use POST"}), 405
                else:
                    posted = _read_body(handler)
                    if not posted.strip():
                        body, code = json.dumps({"error": "empty body"}), 400
                    else:
                        self.inject_message(group_name, posted)
                        body = json.dumps({"ok": True, "group": group_name, "queued": self.get_queued_count(group_name)})
            elif path.startswith("api/queue/"):
                group_name = unquote(path[len("api/queue/"):])
                body = json.dumps({"group": group_name, "queued": self.get_queued_count(group_name)})
            elif path.startswith("webhook/"):
                group_name = unquote(path[len("webhook/"):])
                if method == "POST":
                    posted = _read_body(handler)
                    if not posted.strip():
                        body, code = json.dumps({"error": "empty body"}), 400
                    else:
                        self.inject_message(group_name, posted)
                        body = json.dumps({"ok": True, "queued": self.get_queued_count(group_name)})
                else:
                    body = self._dequeue_for_group(group_name)
            else:
                body, code = json.dumps({"error": "not found"}), 404
        except Exception as ex:  # noqa: BLE001
            body, code = json.dumps({"error": str(ex)}), 500

        encoded = body.encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        if encoded:
            handler.wfile.write(encoded)


def _read_body(handler: BaseHTTPRequestHandler) -> str:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length <= 0:
        return ""
    return handler.rfile.read(length).decode("utf-8", errors="replace")


def _normalize_group(group_name: str) -> str:
    return group_name.strip()
