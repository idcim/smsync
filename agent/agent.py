"""SMSync agent: watches the EC20 for incoming SMS and uploads them to the server.

Run:  python agent.py   (打包后：smsync-agent.exe)
Conf: config.ini next to this file / the exe (falls back to config.example.ini).
Only third-party dependency: pyserial.
"""

import configparser
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import serial

from modem import Modem
from outbox import Outbox

__version__ = "1.1.0"

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：config.ini 放在 exe 旁边，数据放 %APPDATA%（Program Files 不可写）
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    DATA_DIR = Path(os.environ.get("APPDATA", str(BASE_DIR))) / "SMSyncAgent"
else:
    BASE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = BASE_DIR
    DATA_DIR = BASE_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / "agent.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("smsync.agent")


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for path in (BASE_DIR / "config.ini", BUNDLE_DIR / "config.example.ini"):
        if path.exists():
            cfg.read(path, encoding="utf-8")
            if path.name == "config.example.ini":
                log.warning("config.ini not found, using %s (copy it and set your username/password!)", path)
            return cfg
    sys.exit("no config.ini / config.example.ini found")


class Uploader:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self.url = self.base + "/api/v1/sms"
        self.username = username
        self.password = password
        self.token: str | None = None  # JWT，首次发送前登录获取

    def login(self):
        """用用户名密码换 JWT；429（连续失败被锁定）时等 60 秒再试。"""
        while True:
            req = urllib.request.Request(
                self.base + "/api/v1/auth/token",
                data=json.dumps({"username": self.username, "password": self.password}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    data = json.loads(resp.read())
                self.token = data["access_token"]
                log.info("已登录为 %s", data.get("user", {}).get("username", self.username))
                return
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    log.warning("登录连续失败被锁定，60 秒后重试")
                    time.sleep(60)
                    continue
                raise RuntimeError(f"login failed: HTTP {e.code}")

    def _post(self, payload: dict, timeout: float):
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"server returned HTTP {resp.status}")

    def send(self, payload: dict, timeout: float = 10.0):
        if self.token is None:
            self.login()
        try:
            self._post(payload, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            # JWT 过期：重新登录后重试一次
            log.info("JWT 失效，重新登录后重试")
            self.login()
            self._post(payload, timeout)


class Agent:
    def __init__(self, cfg: configparser.ConfigParser):
        self.modem = Modem(
            cfg.get("modem", "port", fallback="COM9"),
            cfg.getint("modem", "baudrate", fallback=115200),
        )
        self.uploader = Uploader(
            cfg.get("server", "url", fallback="http://127.0.0.1:8000"),
            cfg.get("server", "username", fallback=""),
            cfg.get("server", "password", fallback=""),
        )
        outbox_name = cfg.get("agent", "outbox_db", fallback="outbox.db")
        outbox_path = Path(outbox_name)
        if not outbox_path.is_absolute():
            outbox_path = DATA_DIR / outbox_name
        self.outbox = Outbox(str(outbox_path))
        self.heartbeat_sec = cfg.getint("agent", "heartbeat_sec", fallback=30)
        self.flush_sec = cfg.getint("agent", "flush_sec", fallback=10)
        self._last_heartbeat = 0.0
        self._last_flush = 0.0

    # ---- modem lifecycle -------------------------------------------------

    def connect(self):
        while True:
            try:
                self.modem.open()
                self.modem.init_basic()
                return
            except Exception as e:
                log.error("modem connect failed (%s); retrying in 5s", e)
                self.modem.close()
                time.sleep(5)

    def wait_for_sim(self):
        while True:
            try:
                if self.modem.sim_ready():
                    log.info("SIM ready")
                    return
            except Exception as e:
                log.error("CPIN check failed: %s", e)
                raise
            log.warning("SIM not ready, waiting 10s...")
            time.sleep(10)

    # ---- sms handling ----------------------------------------------------

    def handle_sms(self, sms: dict):
        payload = {
            "sender": sms.get("sender") or "unknown",
            "text": sms.get("text") or "",
            "received_at": sms.get("received_at"),
            "client_msg_id": uuid.uuid4().hex,
        }
        log.info("SMS from %s: %.40s", payload["sender"], payload["text"])
        try:
            self.uploader.send(payload)
            log.info("uploaded")
        except Exception as e:
            log.warning("upload failed (%s); queued locally", e)
            self.outbox.enqueue(payload["client_msg_id"], payload)

    def process_index(self, index: int):
        sms = self.modem.read_sms(index)
        if sms:
            self.handle_sms(sms)
        # Always delete: unparseable messages must not jam the SIM storage.
        self.modem.delete_sms(index)

    def drain_stored(self):
        stored = self.modem.list_all()
        if stored:
            log.info("draining %d stored message(s)", len(stored))
        for index, sms in stored:
            self.handle_sms(sms)
            self.modem.delete_sms(index)

    def flush_outbox(self):
        due = self.outbox.due()
        for row_id, payload, attempts in due:
            try:
                self.uploader.send(payload)
                self.outbox.done(row_id)
                log.info("re-sent queued SMS %s", payload["client_msg_id"][:8])
            except Exception as e:
                self.outbox.fail(row_id, attempts)
                log.warning("retry failed (%s), %d still queued", e, self.outbox.size())

    # ---- main loop -------------------------------------------------------

    def run(self):
        while True:
            try:
                self.connect()
                self.wait_for_sim()
                self.modem.init_sms()
                self.drain_stored()
                self.loop()
            except (serial.SerialException, OSError) as e:
                log.error("serial error (%s); reconnecting in 5s", e)
                self.modem.close()
                time.sleep(5)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("unexpected error; restarting in 5s")
                self.modem.close()
                time.sleep(5)

    def loop(self):
        log.info("listening for incoming SMS...")
        while True:
            urc = self.modem.poll_urc(timeout=1.0)
            now = time.monotonic()
            if urc:
                index = Modem.cmti_index(urc)
                if index is not None:
                    self.process_index(index)
                elif urc.startswith(("+CMT", "^")):
                    log.info("URC: %s", urc)
            if now - self._last_heartbeat >= self.heartbeat_sec:
                self._last_heartbeat = now
                log.info("heartbeat: %s", self.modem.signal())
            if now - self._last_flush >= self.flush_sec:
                self._last_flush = now
                self.flush_outbox()


def main():
    cfg = load_config()
    log.info("smsync-agent v%s starting (data dir: %s)", __version__, DATA_DIR)
    try:
        Agent(cfg).run()
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
