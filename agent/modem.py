"""Minimal AT-command layer for the Quectel EC20 SMS functions.

Text mode (AT+CMGF=1) with UCS2 charset so Chinese content arrives as hex.
Single-threaded: URC lines (+CMTI) seen while waiting for a command response
are buffered in self.pending_urcs for the caller to process afterwards.
"""

import csv
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import serial

log = logging.getLogger("smsync.modem")

_FINAL_OK = "OK"
_FINAL_ERR = ("ERROR", "+CME ERROR", "+CMS ERROR")


def decode_field(value: str) -> str:
    """Decode a quoted field that may be UCS2 hex (CSCS=UCS2) or plain text."""
    v = value.strip().strip('"')
    if re.fullmatch(r"[0-9A-Fa-f]+", v) and len(v) >= 4 and len(v) % 4 == 0:
        try:
            return bytes.fromhex(v).decode("utf-16-be")
        except (UnicodeDecodeError, ValueError):
            pass
    return v


def parse_scts(scts: str) -> str:
    """'24/09/25,12:00:00+32' -> ISO 8601. Falls back to the raw string."""
    raw = scts.strip().strip('"')
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{2}),(\d{2}):(\d{2}):(\d{2})([+-]\d{2})", raw)
    if not m:
        return raw
    yy, mo, dd, hh, mi, ss, tz = m.groups()
    tz_minutes = int(tz) * 15
    sign = 1 if tz_minutes >= 0 else -1
    offset = timezone(sign * timedelta(minutes=abs(tz_minutes)))
    dt = datetime(2000 + int(yy), int(mo), int(dd), int(hh), int(mi), int(ss), tzinfo=offset)
    return dt.isoformat(timespec="seconds")


def _split_fields(s: str) -> list[str]:
    # csv keeps empty fields and handles commas inside quotes, e.g. the
    # timestamp in ',"REC UNREAD","<oa>",,"26/09/25,14:30:00+32"'
    return next(csv.reader([s.strip()], skipinitialspace=True))


def parse_cmgr(lines: list[str]) -> Optional[dict]:
    """Parse the response body of AT+CMGR (without the final OK)."""
    header_idx = next((i for i, l in enumerate(lines) if l.startswith("+CMGR:")), None)
    if header_idx is None:
        return None
    fields = _split_fields(lines[header_idx][len("+CMGR:"):])
    body = "".join(l.strip() for l in lines[header_idx + 1:] if not l.startswith("+CM"))
    sender = decode_field(fields[1]) if len(fields) > 1 else ""
    scts = parse_scts(fields[3]) if len(fields) > 3 else None
    return {"sender": sender, "text": decode_field(body), "received_at": scts}


def parse_cmgl(lines: list[str]) -> list[tuple[int, dict]]:
    """Parse AT+CMGL=\"ALL\" output into [(storage_index, sms), ...]."""
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+CMGL:"):
            fields = _split_fields(line[len("+CMGL:"):])
            idx = int(fields[0])
            j = i + 1
            body_lines = []
            while j < len(lines) and not lines[j].startswith("+CMGL:"):
                body_lines.append(lines[j])
                j += 1
            sender = decode_field(fields[2]) if len(fields) > 2 else ""
            scts = parse_scts(fields[4]) if len(fields) > 4 else None
            body = "".join(l.strip() for l in body_lines)
            out.append((idx, {"sender": sender, "text": decode_field(body), "received_at": scts}))
            i = j
        else:
            i += 1
    return out


class Modem:
    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self.ser: Optional[serial.Serial] = None
        self.pending_urcs: list[str] = []

    def open(self):
        self.close()
        self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        log.info("opened %s @ %d", self.port, self.baudrate)

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _readline(self, timeout: float) -> str:
        assert self.ser is not None
        self.ser.timeout = timeout
        raw = self.ser.readline()
        return raw.decode("ascii", errors="replace").strip()

    def _write(self, cmd: str):
        assert self.ser is not None
        self.ser.write(cmd.encode("ascii") + b"\r")

    def _wait_final(self, timeout: float) -> tuple[bool, list[str]]:
        """等到 OK/ERROR 终态行；期间收到的 URC 行缓冲起来。"""
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._readline(max(0.1, deadline - time.monotonic()))
            if not line:
                continue
            if line == _FINAL_OK:
                return True, lines
            if any(line.startswith(e) for e in _FINAL_ERR):
                return False, lines + [line]
            if line.startswith("+CMTI") or line.startswith("+CMT") or line.startswith("^"):
                self.pending_urcs.append(line)
                continue
            lines.append(line)
        raise serial.SerialTimeoutException("timeout waiting for modem response")

    def command(self, cmd: str, timeout: float = 5.0) -> tuple[bool, list[str]]:
        """Send a command, return (ok, response_lines). URCs are buffered."""
        self._write(cmd)
        try:
            return self._wait_final(timeout)
        except serial.SerialTimeoutException:
            raise serial.SerialTimeoutException(f"timeout waiting for response to {cmd!r}")

    def poll_urc(self, timeout: float = 1.0) -> Optional[str]:
        """Read one unsolicited line (returns None on timeout)."""
        if self.pending_urcs:
            return self.pending_urcs.pop(0)
        line = self._readline(timeout)
        return line or None

    def init_basic(self):
        for cmd in ("AT", "ATE0"):
            ok, resp = self.command(cmd)
            if not ok:
                raise RuntimeError(f"{cmd!r} failed: {resp}")

    def init_sms(self):
        # These require a ready SIM; call only after sim_ready().
        for cmd in ("AT+CMGF=1", 'AT+CSCS="UCS2"',
                    'AT+CPMS="SM","SM","SM"', "AT+CNMI=2,1,0,0,0"):
            ok, resp = self.command(cmd)
            if not ok:
                raise RuntimeError(f"{cmd!r} failed: {resp}")
        log.info("modem initialised (text mode, UCS2, CNMI push enabled)")

    def sim_ready(self) -> bool:
        ok, resp = self.command("AT+CPIN?")
        return ok and any("READY" in l for l in resp)

    def signal(self) -> str:
        ok, resp = self.command("AT+CSQ")
        return resp[0] if ok and resp else "unknown"

    def read_sms(self, index: int) -> Optional[dict]:
        ok, lines = self.command(f"AT+CMGR={index}", timeout=10.0)
        if not ok:
            log.warning("AT+CMGR=%d failed: %s", index, lines)
            return None
        return parse_cmgr(lines)

    def delete_sms(self, index: int) -> bool:
        ok, _ = self.command(f"AT+CMGD={index}")
        return ok

    def list_all(self) -> list[tuple[int, dict]]:
        ok, lines = self.command('AT+CMGL="ALL"', timeout=20.0)
        if not ok:
            log.warning("AT+CMGL failed: %s", lines)
            return []
        return parse_cmgl(lines)

    def send_sms(self, number: str, text: str, timeout: float = 30.0) -> tuple[bool, str]:
        """UCS2 文本模式发短信（参考 agent_rpi/modem.py）。返回 (ok, 错误或 +CMGS 引用)。"""
        da = number.encode("utf-16-be").hex().upper()
        body = text.encode("utf-16-be").hex().upper()
        self._write(f'AT+CMGS="{da}"')
        # 等待 "> " 提示符（无 CR/LF，按原始字节读）
        assert self.ser is not None
        self.ser.timeout = 10.0
        prompt = self.ser.read_until(b">", size=64)
        if b">" not in prompt:
            return False, "no CMGS prompt"
        self.ser.write(body.encode("ascii") + b"\x1a")  # ctrl-Z 结束输入
        ok, lines = self._wait_final(timeout)
        if ok:
            ref = next((l for l in lines if l.startswith("+CMGS:")), "sent")
            return True, ref
        return False, "; ".join(lines) or "send failed"

    @staticmethod
    def cmti_index(urc: str) -> Optional[int]:
        m = re.search(r"\+CMTI:\s*\"\w+\",\s*(\d+)", urc)
        return int(m.group(1)) if m else None
