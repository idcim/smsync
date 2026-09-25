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

from auth import JwtAuth

log = logging.getLogger("smsync.uplink")


def build_ws_url(base: str, token: str) -> str:
    parts = urlsplit(base.rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, parts.path + "/ws/agent",
                       urlencode({"token": token}), ""))


class Uplink:
    def __init__(self, base_url: str, auth: JwtAuth,
                 handler: Callable[[str, dict], tuple[bool, str]]):
        """handler(action, params) -> (ok, error)."""
        self.base = base_url
        self.auth = auth
        self.handler = handler
        self._ws: WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._close_code: int | None = None

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

    def _on_close(self, ws, code, msg):
        log.warning("uplink closed (code=%s)", code)
        self._close_code = code

    def _run(self):
        while True:
            try:
                url = build_ws_url(self.base, self.auth.token())
            except Exception as e:
                # 登录失败（如密码错误、服务器不可达）：稍后重试，线程不退
                log.warning("login failed (%s); retrying in 30s", e)
                time.sleep(30)
                continue
            self._close_code = None
            self._ws = WebSocketApp(
                url,
                on_open=lambda ws: log.info("uplink connected"),
                on_message=self._on_message,
                on_close=self._on_close,
                on_error=lambda ws, e: log.warning("uplink error: %s", e),
            )
            self._ws.run_forever(ping_interval=25, ping_timeout=10)
            if self._close_code == 4401:
                # JWT 无效/过期：重新登录拿新 JWT，下一轮用新令牌重连
                log.info("JWT 已失效，重新登录")
                try:
                    self.auth.refresh()
                except Exception as e:
                    log.warning("re-login failed: %s", e)
            time.sleep(5)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
