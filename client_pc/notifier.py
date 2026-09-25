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
                log.warning("config.ini not found, using %s (copy it and set your token!)", name)
            return cfg
    sys.exit("no config.ini / config.example.ini found in client_pc/")


def build_ws_url(base: str, token: str) -> str:
    parts = urlsplit(base.rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, parts.path + "/ws", urlencode({"token": token}), ""))


def main():
    cfg = load_config()
    url = build_ws_url(
        cfg.get("server", "url", fallback="http://127.0.0.1:8000"),
        cfg.get("server", "token", fallback=""),
    )

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
        log.info("connected to %s", url.split("?")[0])
        threading.Thread(
            target=lambda: [ws.send("ping") or time.sleep(25) for _ in iter(int, 1)],
            daemon=True,
        ).start()

    def on_close(ws, code, msg):
        log.warning("disconnected (code=%s)", code)

    while True:
        WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=lambda ws, e: log.warning("ws error: %s", e),
        ).run_forever(ping_interval=0)
        log.info("reconnecting in 5s...")
        time.sleep(5)


if __name__ == "__main__":
    main()
