"""Voice-call state machine driven by URCs and AT+CLCC polling.

States: idle -> ringing (incoming) / dialing (outgoing) -> active -> idle.
Every transition produces an event dict that the agent uploads to the server;
recording starts when the call becomes active and stops when it ends.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("smsync.voice")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CallManager:
    def __init__(self,
                 on_event: Callable[[dict], None],
                 on_call_end: Callable[[dict], None]):
        """on_event: called on every status change (upload to server).
        on_call_end: called once when a call fully ends (stop/upload recording)."""
        self.on_event = on_event
        self.on_call_end = on_call_end
        self.call: Optional[dict] = None

    @property
    def busy(self) -> bool:
        return self.call is not None

    @property
    def status(self) -> str:
        return self.call["status"] if self.call else "idle"

    def _transition(self, status: str, **extra):
        assert self.call is not None
        self.call["status"] = status
        self.call.update(extra)
        log.info("call %s -> %s (%s)", self.call["client_msg_id"][:8], status, self.call["number"])
        self.on_event(dict(self.call))

    def _finish(self, status: str):
        call, self.call = self.call, None
        assert call is not None
        call["status"] = status
        call["ended_at"] = _now()
        if call.get("answered_at"):
            try:
                t0 = datetime.fromisoformat(call["answered_at"])
                t1 = datetime.fromisoformat(call["ended_at"])
                call["duration"] = max(0, int((t1 - t0).total_seconds()))
            except ValueError:
                pass
        log.info("call ended: %s (%s)", call["number"], status)
        self.on_event(dict(call))
        self.on_call_end(call)

    # ---- URC-driven events ----

    def on_ring(self):
        if self.call is None:
            self.call = {
                "client_msg_id": uuid.uuid4().hex,
                "direction": "in",
                "number": "",
                "status": "ringing",
                "started_at": _now(),
            }
            self.on_event(dict(self.call))

    def on_clip(self, number: str):
        if self.call and self.call["direction"] == "in" and not self.call["number"]:
            self.call["number"] = number or "unknown"
            self.on_event(dict(self.call))

    def on_call_end_urc(self, reason: str):
        if self.call is None:
            return
        status = {
            "ringing": "missed",
            "dialing": "failed",
            "alerting": "failed",
        }.get(self.call["status"], "ended")
        self._finish(status)

    # ---- command-driven events ----

    def on_dial(self, number: str):
        if self.call is not None:
            raise RuntimeError("already in a call")
        self.call = {
            "client_msg_id": uuid.uuid4().hex,
            "direction": "out",
            "number": number,
            "status": "dialing",
            "started_at": _now(),
        }
        self.on_event(dict(self.call))

    def on_answered(self):
        if self.call and self.call["status"] in ("ringing",):
            self._transition("active", answered_at=_now())

    # ---- CLCC polling (called ~every 2s while busy) ----

    def update_from_clcc(self, calls: list[dict]):
        if self.call is None:
            return
        if not calls:
            # Modem reports no calls: ours ended (remote hangup).
            if self.call["status"] in ("active", "held"):
                self._finish("ended")
            elif self.call["status"] in ("dialing", "alerting"):
                self._finish("failed")
            elif self.call["status"] == "ringing":
                self._finish("missed")
            return
        mine = calls[0]
        if self.call["status"] in ("dialing", "alerting") and mine["state"] == "active":
            self._transition("active", answered_at=_now())
        elif self.call["status"] == "dialing" and mine["state"] == "alerting":
            self._transition("alerting")
