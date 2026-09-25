"""SMSync PC notifier: connects to the server's WebSocket and pops a Windows
toast for every incoming SMS. Falls back to console output if toasts are
unavailable. Auto-reconnects forever.

Run:  python notifier.py
Conf: config.ini next to this file (falls back to config.example.ini).
"""

import configparser
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from websocket import WebSocketApp

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("smsync.notifier")

try:
    from windows_toasts import Toast, WindowsToaster
    _toaster = WindowsToaster("SMSync")
except Exception as e:  # pragma: no cover - depends on desktop session
    log.warning("windows-toasts unavailable (%s), printing to console instead", e)
    _toaster = None


def notify(sender: str, text: str):
    if _toaster is not None:
        try:
            _toaster.show_toast(Toast([f"新短信：{sender}", text]))
            return
        except Exception as e:
            log.warning("toast failed (%s)", e)
    print(f"\n=== 新短信：{sender} ===\n{text}\n")


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for name in ("config.ini", "config.example.ini"):
        path = BASE_DIR / name
        if path.exists():
            cfg.read(path, encoding="utf-8")
            if name == "config.example.ini":
                log.warning("config.ini not found, using %s (copy it and set your username/password!)", name)
            return cfg
    sys.exit("no config.ini / config.example.ini found in client_pc/")


def build_ws_url(base: str, token: str) -> str:
    parts = urlsplit(base.rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, parts.path + "/ws", urlencode({"token": token}), ""))


def login(base: str, username: str, password: str) -> str:
    """用用户名密码换 JWT；429（连续失败被锁定）时等 60 秒再试。"""
    while True:
        req = urllib.request.Request(
            base + "/api/v1/auth/token",
            data=json.dumps({"username": username, "password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                data = json.loads(resp.read())
            log.info("已登录为 %s", data.get("user", {}).get("username", username))
            return data["access_token"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                log.warning("登录连续失败被锁定，60 秒后重试")
                time.sleep(60)
                continue
            raise RuntimeError(f"login failed: HTTP {e.code}")


def main():
    cfg = load_config()
    base = cfg.get("server", "url", fallback="http://127.0.0.1:8000").rstrip("/")
    username = cfg.get("server", "username", fallback="")
    password = cfg.get("server", "password", fallback="")
    token: str | None = None
    closed = {"code": None}

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if data.get("type") == "sms":
            sms = data["data"]
            log.info("SMS from %s", sms.get("sender"))
            notify(sms.get("sender", "unknown"), sms.get("text", ""))

    def on_open(ws):
        log.info("connected to %s", base + "/ws")
        threading.Thread(
            target=lambda: [ws.send("ping") or time.sleep(25) for _ in iter(int, 1)],
            daemon=True,
        ).start()

    def on_close(ws, code, msg):
        log.warning("disconnected (code=%s)", code)
        closed["code"] = code

    while True:
        if token is None:
            try:
                token = login(base, username, password)
            except Exception as e:
                # 登录失败（如密码错误、服务器不可达）：稍后重试
                log.error("登录失败（%s），30 秒后重试", e)
                time.sleep(30)
                continue
        closed["code"] = None
        WebSocketApp(
            build_ws_url(base, token),
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=lambda ws, e: log.warning("ws error: %s", e),
        ).run_forever(ping_interval=0)
        if closed["code"] == 4401:
            # JWT 无效/过期：丢弃旧令牌，下一轮重新登录
            log.info("JWT 已失效，重新登录")
            token = None
        log.info("reconnecting in 5s...")
        time.sleep(5)


if __name__ == "__main__":
    main()
