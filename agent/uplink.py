"""下行指令通道：连服务器 /ws/agent，接收并执行指令（send_sms 等）。

参考 agent_rpi/uplink.py：断线 5s 退避重连；4401（JWT 失效）用 refresh
token 续期后再连。token 统一由 Uploader 持有（线程安全），此处不自己登录。
"""

import json
import logging
import threading
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
    def __init__(self, uploader, handler: Callable[[str, dict], tuple[bool, str]],
                 stop: threading.Event, on_status=lambda text: None):
        """handler(action, params) -> (ok, error)；token 取自 uploader。"""
        self.uploader = uploader
        self.handler = handler
        self._stop = stop
        self._status = on_status
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

    def _on_open(self, ws):
        log.info("uplink connected")
        self._status("已上线（指令通道在线）")

    def _on_close(self, ws, code, msg):
        log.warning("uplink closed (code=%s)", code)
        self._close_code = code

    def _run(self):
        while not self._stop.is_set():
            try:
                url = build_ws_url(self.uploader.base, self.uploader.access_token())
            except Exception as e:
                # 认证失败（设备码无效/服务器不可达）：稍后重试，线程不退
                log.warning("get token failed (%s); retrying in 30s", e)
                self._stop.wait(30)
                continue
            self._close_code = None
            self._ws = WebSocketApp(
                url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_close=self._on_close,
                on_error=lambda ws, e: log.warning("uplink error: %s", e),
            )
            self._ws.run_forever(ping_interval=25, ping_timeout=10)
            if self._stop.is_set():
                break
            if self._close_code == 4401:
                # JWT 过期：refresh 续期，下一轮用新 token 重连
                log.info("JWT 已失效，refresh 续期后重连")
                self.uploader.refresh()
            self._stop.wait(5)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="uplink")
        self._thread.start()

    def stop(self):
        """关闭当前 WS 打断 run_forever，并等线程退出。"""
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                log.warning("uplink thread did not stop in time")
