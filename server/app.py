import asyncio
import contextlib
import json
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import BASE_DIR, DB_PATH, RECORDINGS_DIR, TOKEN
from db import Database, to_event

app = FastAPI(title="SMSync", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
db = Database(DB_PATH)


class SmsIn(BaseModel):
    sender: str
    text: str
    received_at: Optional[str] = None
    client_msg_id: Optional[str] = None


class SmsSendIn(BaseModel):
    to: str
    text: str


class CallEventIn(BaseModel):
    client_msg_id: str
    direction: str  # "in" | "out"
    number: str
    status: str  # ringing / dialing / active / missed / ended / failed
    started_at: Optional[str] = None
    answered_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration: Optional[int] = None


class DialIn(BaseModel):
    number: str


def check_token(authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="invalid token")


class WsHub:
    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket):
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket):
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: str):
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                with contextlib.suppress(Exception):
                    await self.remove(ws)


hub = WsHub()


class AgentOffline(Exception):
    pass


class AgentChannel:
    """Downlink to the modem agent (RPi). The agent keeps one persistent WS
    at /ws/agent; client commands are forwarded and answered by ack id."""

    def __init__(self):
        self.ws: Optional[WebSocket] = None
        self.pending: dict[str, asyncio.Future] = {}

    @property
    def online(self) -> bool:
        return self.ws is not None

    async def attach(self, ws: WebSocket):
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close(code=4000)
        self.ws = ws

    def detach(self, ws: WebSocket):
        if self.ws is ws:
            self.ws = None
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(AgentOffline("agent disconnected"))
            self.pending.clear()

    async def command(self, action: str, timeout: float = 20.0, **params) -> dict:
        if not self.ws:
            raise AgentOffline()
        cmd_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending[cmd_id] = fut
        try:
            await self.ws.send_text(json.dumps(
                {"id": cmd_id, "action": action, **params}, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(cmd_id, None)

    def handle_message(self, data: dict):
        ack_id = data.get("ack")
        if ack_id and ack_id in self.pending:
            fut = self.pending[ack_id]
            if not fut.done():
                fut.set_result(data)


agent_channel = AgentChannel()


@app.get("/api/v1/health")
def health():
    return {"ok": True}


@app.post("/api/v1/sms", status_code=201, dependencies=[Depends(check_token)])
async def create_sms(payload: SmsIn):
    row, created = db.insert_sms(
        sender=payload.sender,
        text=payload.text,
        received_at=payload.received_at,
        client_msg_id=payload.client_msg_id,
    )
    if created:
        await hub.broadcast(to_event(row))
    return row


@app.get("/api/v1/sms", dependencies=[Depends(check_token)])
def list_sms(
    limit: int = Query(default=50, ge=1, le=500),
    before_id: Optional[int] = None,
):
    return {"items": db.list_sms(limit=limit, before_id=before_id)}


@app.get("/api/v1/sms/{sms_id}", dependencies=[Depends(check_token)])
def get_sms(sms_id: int):
    row = db.get_sms(sms_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return row


@app.delete("/api/v1/sms/{sms_id}", dependencies=[Depends(check_token)])
async def delete_sms(sms_id: int):
    if not db.delete_sms(sms_id):
        raise HTTPException(status_code=404, detail="not found")
    await hub.broadcast(json.dumps({"type": "delete", "data": {"id": sms_id}}))
    return {"deleted": sms_id}


# ---- sms sending (downlink via agent) ----

@app.post("/api/v1/sms/send", dependencies=[Depends(check_token)])
async def send_sms(payload: SmsSendIn):
    row = db.insert_outbox_sms(uuid.uuid4().hex, payload.to, payload.text)
    try:
        ack = await agent_channel.command(
            "send_sms", to=payload.to, text=payload.text,
            client_msg_id=row["client_msg_id"])
        ok, error = bool(ack.get("ok")), ack.get("error")
    except AgentOffline:
        ok, error = False, "agent offline"
    row = db.update_outbox_status(row["client_msg_id"], "sent" if ok else "failed", error)
    await hub.broadcast(json.dumps({"type": "sms_sent", "data": row}, ensure_ascii=False))
    if not ok:
        raise HTTPException(status_code=503 if error == "agent offline" else 502,
                            detail=error or "send failed")
    return row


@app.get("/api/v1/sms/outbox/list", dependencies=[Depends(check_token)])
def list_outbox(limit: int = Query(default=50, ge=1, le=500)):
    return {"items": db.list_outbox_sms(limit=limit)}


# ---- calls ----

@app.post("/api/v1/calls", status_code=201, dependencies=[Depends(check_token)])
async def report_call(event: CallEventIn):
    row = db.upsert_call(event.client_msg_id, **event.model_dump(exclude={"client_msg_id"}))
    await hub.broadcast(json.dumps({"type": "call", "data": row}, ensure_ascii=False))
    return row


@app.get("/api/v1/calls", dependencies=[Depends(check_token)])
def list_calls(limit: int = Query(default=50, ge=1, le=500)):
    return {"items": db.list_calls(limit=limit)}


@app.post("/api/v1/calls/dial", dependencies=[Depends(check_token)])
async def dial(payload: DialIn):
    try:
        ack = await agent_channel.command("dial", number=payload.number)
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "dial failed")
    return ack


@app.post("/api/v1/calls/answer", dependencies=[Depends(check_token)])
async def answer_call():
    try:
        ack = await agent_channel.command("answer")
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "answer failed")
    return ack


@app.post("/api/v1/calls/hangup", dependencies=[Depends(check_token)])
async def hangup_call():
    try:
        ack = await agent_channel.command("hangup")
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "hangup failed")
    return ack


@app.post("/api/v1/calls/{call_id}/recording", dependencies=[Depends(check_token)])
async def upload_recording(call_id: int, request: Request):
    if not db.get_call(call_id):
        raise HTTPException(status_code=404, detail="not found")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    filename = f"call_{call_id}.wav"
    (RECORDINGS_DIR / filename).write_bytes(data)
    row = db.set_recording(call_id, filename)
    await hub.broadcast(json.dumps({"type": "call", "data": row}, ensure_ascii=False))
    return {"ok": True, "size": len(data)}


@app.get("/api/v1/calls/{call_id}/recording", dependencies=[Depends(check_token)])
def get_recording(call_id: int):
    call = db.get_call(call_id)
    if not call or not call.get("recording_file"):
        raise HTTPException(status_code=404, detail="no recording")
    path = RECORDINGS_DIR / call["recording_file"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="recording file missing")
    return FileResponse(path, media_type="audio/wav", filename=call["recording_file"])


@app.websocket("/ws")
async def ws(websocket: WebSocket, token: str = ""):
    if token != TOKEN:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await hub.add(websocket)
    try:
        while True:
            # Keep the connection alive; clients may send pings we ignore.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove(websocket)


@app.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket, token: str = ""):
    if token != TOKEN:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await agent_channel.attach(websocket)
    await hub.broadcast(json.dumps({"type": "agent", "data": {"online": True}}))
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                agent_channel.handle_message(json.loads(raw))
            except (json.JSONDecodeError, AttributeError):
                pass
    except WebSocketDisconnect:
        pass
    finally:
        agent_channel.detach(websocket)
        await hub.broadcast(json.dumps({"type": "agent", "data": {"online": False}}))


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
