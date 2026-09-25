"""SMSync agent: watches the EC20 for incoming SMS and uploads them to the server.

Run:  python agent.py
Conf: config.ini next to this file (falls back to config.example.ini).
Only third-party dependency: pyserial.
"""

import configparser
import json
import logging
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import serial

from modem import Modem
from outbox import Outbox

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("smsync.agent")


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for name in ("config.ini", "config.example.ini"):
        path = BASE_DIR / name
        if path.exists():
            cfg.read(path, encoding="utf-8")
            if name == "config.example.ini":
                log.warning("config.ini not found, using %s (copy it and set your token!)", name)
            return cfg
    sys.exit("no config.ini / config.example.ini found in agent/")


class Uploader:
    def __init__(self, base_url: str, token: str):
        self.url = base_url.rstrip("/") + "/api/v1/sms"
        self.token = token

    def send(self, payload: dict, timeout: float = 10.0):
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


class Agent:
    def __init__(self, cfg: configparser.ConfigParser):
        self.modem = Modem(
            cfg.get("modem", "port", fallback="COM9"),
            cfg.getint("modem", "baudrate", fallback=115200),
        )
        self.uploader = Uploader(
            cfg.get("server", "url", fallback="http://127.0.0.1:8000"),
            cfg.get("server", "token", fallback=""),
        )
        self.outbox = Outbox(str(BASE_DIR / cfg.get("agent", "outbox_db", fallback="outbox.db")))
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
    try:
        Agent(cfg).run()
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
