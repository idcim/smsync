"""Downlink command channel: persistent WebSocket to the server.

The server forwards client commands (dial/answer/hangup/send_sms) here;
each command is executed and acked by id. Reconnects forever.
"""

import json
import logging
import threading
import time
from typing import Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

from websocket import WebSocketApp

log = logging.getLogger("smsync.uplink")


def build_ws_url(base: str, token: str) -> str:
    parts = urlsplit(base.rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, parts.path + "/ws/agent",
                       urlencode({"token": token}), ""))


class Uplink:
    def __init__(self, base_url: str, token: str,
                 handler: Callable[[str, dict], tuple[bool, str]]):
        """handler(action, params) -> (ok, error)."""
        self.url = build_ws_url(base_url, token)
        self.handler = handler
        self._ws: WebSocketApp | None = None
        self._thread: threading.Thread | None = None

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        cmd_id, action = data.get("id"), data.get("action")
        if not cmd_id or not action:
            return
        log.info("command: %s %s", action, {k: v for k, v in data.items() if k not in ("id", "action")})
        try:
            ok, error = self.handler(action, data)
        except Exception as e:
            log.exception("command %s raised", action)
            ok, error = False, str(e)
        try:
            ws.send(json.dumps({"ack": cmd_id, "ok": ok, "error": error or None}))
        except Exception as e:
            log.warning("ack send failed: %s", e)

    def _run(self):
        while True:
            self._ws = WebSocketApp(
                self.url,
                on_open=lambda ws: log.info("uplink connected"),
                on_message=self._on_message,
                on_close=lambda ws, c, m: log.warning("uplink closed (code=%s)", c),
                on_error=lambda ws, e: log.warning("uplink error: %s", e),
            )
            self._ws.run_forever(ping_interval=25, ping_timeout=10)
            time.sleep(5)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
