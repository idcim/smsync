import asyncio
import contextlib
import json
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import BASE_DIR, DB_PATH, TOKEN
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


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
