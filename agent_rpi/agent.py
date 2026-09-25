"""SMSync RPi agent: SMS send/receive + voice call control + call recording.

Runs on the Raspberry Pi connected to the EC20. Receives modem events and
uploads them to the server; executes downlink commands (dial/answer/hangup/
send_sms) arriving over the uplink WebSocket.

Run:  python agent.py
Conf: config.ini next to this file (falls back to config.example.ini).
"""

import configparser
import json
import logging
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import serial

from audio import CallRecorder
from modem import Modem, classify_urc
from outbox import Outbox
from uplink import Uplink
from voice import CallManager

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
    sys.exit("no config.ini / config.example.ini found in agent_rpi/")


class Api:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, body=None, raw=False, timeout=15.0):
        headers = {"Authorization": f"Bearer {self.token}"}
        data = body
        if raw:
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if resp.status >= 300:
                raise RuntimeError(f"server returned HTTP {resp.status}")
            return json.loads(payload) if payload and not raw else payload

    def post_sms(self, payload: dict):
        return self._request("POST", "/api/v1/sms", payload)

    def post_call(self, event: dict) -> dict:
        return self._request("POST", "/api/v1/calls", event)

    def upload_recording(self, call_id: int, wav: bytes):
        return self._request("POST", f"/api/v1/calls/{call_id}/recording", wav, raw=True, timeout=60.0)


class Agent:
    def __init__(self, cfg: configparser.ConfigParser):
        self.modem = Modem(
            cfg.get("modem", "port", fallback="/dev/ttyUSB2"),
            cfg.getint("modem", "baudrate", fallback=115200),
        )
        self.modem_lock = threading.Lock()
        self.api = Api(
            cfg.get("server", "url", fallback="http://127.0.0.1:8000"),
            cfg.get("server", "token", fallback=""),
        )
        self.outbox = Outbox(str(BASE_DIR / cfg.get("agent", "outbox_db", fallback="outbox.db")))
        self.heartbeat_sec = cfg.getint("agent", "heartbeat_sec", fallback=30)
        self.flush_sec = cfg.getint("agent", "flush_sec", fallback=10)
        self.clcc_poll_sec = cfg.getint("agent", "clcc_poll_sec", fallback=2)

        self.recorder = CallRecorder(
            device=cfg.get("audio", "device", fallback="default"),
            rate=cfg.getint("audio", "rate", fallback=8000),
            channels=cfg.getint("audio", "channels", fallback=1),
            out_dir=str(BASE_DIR / cfg.get("audio", "dir", fallback="recordings")),
        )
        self.audio_enabled = cfg.getboolean("audio", "enabled", fallback=True)

        self._call_server_ids: dict[str, int] = {}
        self.voice = CallManager(on_event=self._on_call_event, on_call_end=self._on_call_end)
        self.uplink = Uplink(self.api.base, self.api.token, self.execute_command)

        self._last_heartbeat = 0.0
        self._last_flush = 0.0
        self._last_clcc = 0.0

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
            with self.modem_lock:
                ready = self.modem.sim_ready()
            if ready:
                log.info("SIM ready")
                return
            log.warning("SIM not ready, waiting 10s...")
            time.sleep(10)

    # ---- sms -------------------------------------------------------------

    def handle_sms(self, sms: dict):
        payload = {
            "sender": sms.get("sender") or "unknown",
            "text": sms.get("text") or "",
            "received_at": sms.get("received_at"),
            "client_msg_id": uuid.uuid4().hex,
        }
        log.info("SMS from %s: %.40s", payload["sender"], payload["text"])
        try:
            self.api.post_sms(payload)
            log.info("uploaded")
        except Exception as e:
            log.warning("upload failed (%s); queued locally", e)
            self.outbox.enqueue(payload["client_msg_id"], payload)

    def process_index(self, index: int):
        with self.modem_lock:
            sms = self.modem.read_sms(index)
            self.modem.delete_sms(index)
        if sms:
            self.handle_sms(sms)

    def drain_stored(self):
        with self.modem_lock:
            stored = self.modem.list_all()
        if stored:
            log.info("draining %d stored message(s)", len(stored))
        for index, sms in stored:
            self.handle_sms(sms)
            with self.modem_lock:
                self.modem.delete_sms(index)

    def flush_outbox(self):
        for row_id, payload, attempts in self.outbox.due():
            try:
                self.api.post_sms(payload)
                self.outbox.done(row_id)
                log.info("re-sent queued SMS %s", payload["client_msg_id"][:8])
            except Exception as e:
                self.outbox.fail(row_id, attempts)
                log.warning("retry failed (%s), %d still queued", e, self.outbox.size())

    # ---- calls -------------------------------------------------------------

    def _on_call_event(self, event: dict):
        try:
            row = self.api.post_call(event)
            self._call_server_ids[event["client_msg_id"]] = row["id"]
        except Exception as e:
            log.warning("call event upload failed: %s", e)
        if event["status"] == "active" and self.audio_enabled and not self.recorder.recording:
            self.recorder.start()

    def _on_call_end(self, call: dict):
        wav = self.recorder.stop() if self.audio_enabled else None
        if not wav:
            return
        call_id = self._call_server_ids.get(call["client_msg_id"])
        if not call_id:
            log.warning("no server id for call, cannot upload recording")
            return
        try:
            self.api.upload_recording(call_id, wav.read_bytes())
            log.info("recording uploaded for call %d", call_id)
            wav.unlink()
        except Exception as e:
            log.warning("recording upload failed (%s); kept at %s", e, wav)

    # ---- downlink commands -------------------------------------------------

    def execute_command(self, action: str, params: dict) -> tuple[bool, str]:
        if action == "send_sms":
            with self.modem_lock:
                ok, info = self.modem.send_sms(params["to"], params["text"])
            return ok, "" if ok else info
        if action == "dial":
            if self.voice.busy:
                return False, "already in a call"
            self.voice.on_dial(params["number"])
            with self.modem_lock:
                ok, info = self.modem.dial(params["number"])
            if not ok:
                self.voice.on_call_end_urc("ATD failed")
                return False, info or "dial failed"
            return True, ""
        if action == "answer":
            if self.voice.status != "ringing":
                return False, "no incoming call"
            with self.modem_lock:
                ok, info = self.modem.answer()
            if ok:
                self.voice.on_answered()
            return ok, "" if ok else info
        if action == "hangup":
            if not self.voice.busy:
                return False, "no active call"
            with self.modem_lock:
                ok, info = self.modem.hangup()
            if ok:
                self.voice.on_call_end_urc("hangup")
            return ok, "" if ok else info
        return False, f"unknown action {action!r}"

    # ---- main loop ---------------------------------------------------------

    def run(self):
        while True:
            try:
                self.connect()
                self.wait_for_sim()
                with self.modem_lock:
                    self.modem.init_sms()
                    self.modem.init_voice()
                self.drain_stored()
                self.uplink.start()
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
        log.info("listening for SMS and calls...")
        while True:
            with self.modem_lock:
                urc = self.modem.poll_urc(timeout=1.0)
            now = time.monotonic()
            if urc:
                kind, payload = classify_urc(urc)
                if kind == "cmti":
                    self.process_index(payload)
                elif kind == "ring":
                    self.voice.on_ring()
                elif kind == "clip":
                    self.voice.on_clip(payload)
                elif kind == "call_end":
                    self.voice.on_call_end_urc(payload)
            if self.voice.busy and now - self._last_clcc >= self.clcc_poll_sec:
                self._last_clcc = now
                with self.modem_lock:
                    calls = self.modem.current_calls()
                self.voice.update_from_clcc(calls)
            if now - self._last_heartbeat >= self.heartbeat_sec:
                self._last_heartbeat = now
                with self.modem_lock:
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
