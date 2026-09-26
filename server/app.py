import asyncio
import contextlib
import json
import os
import secrets
import time
import uuid
from typing import Literal, Optional

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import (
    ACCESS_HOURS,
    EXPIRE_HOURS,
    REFRESH_DAYS,
    check_captcha,
    decode_token,
    hash_password,
    make_token,
    new_captcha,
    verify_password,
)
from config import APP_VERSION, BASE_DIR, DB_PATH, RECORDINGS_DIR
from db import Database, to_event

app = FastAPI(title="SMSync", version=APP_VERSION)
# 所有页面与 API 同源（PWA/管理后台由本服务托管），无需放开 CORS；浏览器跨域默认拒绝
db = Database(DB_PATH)

MAX_RECORDING_BYTES = 25 * 1024 * 1024  # 通话录音上传上限 25MB
PHONE_PATTERN = r"^[0-9+*#,]{1,20}$"    # 白名单字符，杜绝 AT 指令注入（agent 直接拼进 ATD）
USERNAME_PATTERN = r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{1,31}$"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


class SmsIn(BaseModel):
    sender: str = Field(max_length=100)
    text: str = Field(max_length=5000)
    received_at: Optional[str] = Field(default=None, max_length=40)
    client_msg_id: Optional[str] = Field(default=None, max_length=64)


class SmsSendIn(BaseModel):
    to: str = Field(pattern=PHONE_PATTERN)
    text: str = Field(min_length=1, max_length=2000)


class CallEventIn(BaseModel):
    client_msg_id: str = Field(max_length=64)
    direction: Literal["in", "out"]
    number: str = Field(max_length=32)
    status: Literal["ringing", "dialing", "alerting", "active", "missed", "ended", "failed"]
    started_at: Optional[str] = Field(default=None, max_length=40)
    answered_at: Optional[str] = Field(default=None, max_length=40)
    ended_at: Optional[str] = Field(default=None, max_length=40)
    duration: Optional[int] = Field(default=None, ge=0)


class DialIn(BaseModel):
    number: str = Field(pattern=PHONE_PATTERN)


class LoginIn(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(min_length=1, max_length=128)
    captcha_id: str = Field(default="", max_length=64)
    captcha_text: str = Field(default="", max_length=8)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class UserCreateIn(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=6, max_length=128)
    role: Literal["admin", "user"] = "user"


class UserUpdateIn(BaseModel):
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)
    role: Optional[Literal["admin", "user"]] = None
    disabled: Optional[bool] = None


# ---- 认证依赖 ----

def _user_from_token(token: str, kind: str = "access") -> dict:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    # typ 缺省按 access 处理（兼容旧版签发的 token）
    if payload.get("typ", "access") != kind:
        raise HTTPException(status_code=401, detail="wrong token type")
    user = db.get_user(int(payload["sub"]))
    if not user or user["disabled"]:
        raise HTTPException(status_code=401, detail="user disabled or deleted")
    return user


def current_user(authorization: str = Header(default="")) -> dict:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return _user_from_token(token)


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def public_user(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


# ---- 登录限流：同一 用户名+IP 连续失败 5 次锁 60 秒 ----

_login_fails: dict[tuple[str, str], tuple[int, float]] = {}


def _login_throttle(username: str, ip: str):
    key = (username, ip)
    fails, lock_until = _login_fails.get(key, (0, 0.0))
    if lock_until > time.monotonic():
        raise HTTPException(status_code=429, detail="too many attempts, try later")
    return key, fails


def bootstrap_admin():
    """首次启动（无任何用户）时创建管理员。密码取 SMSYNC_ADMIN_PASSWORD，
    否则随机生成并写入 server/.admin_credentials（该文件已 gitignore）。"""
    if db.count_users() > 0:
        return
    username = os.environ.get("SMSYNC_ADMIN_USER", "admin")
    password = os.environ.get("SMSYNC_ADMIN_PASSWORD")
    if not password:
        from config import DATA_DIR
        password = secrets.token_urlsafe(12)
        (DATA_DIR / ".admin_credentials").write_text(
            f"username: {username}\npassword: {password}\n", encoding="utf-8")
    db.create_user(username, hash_password(password), "admin")
    print(f"[smsync] created admin user '{username}'"
          + ("" if os.environ.get("SMSYNC_ADMIN_PASSWORD")
             else " (random password in .admin_credentials)"))


bootstrap_admin()


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
    # version 无需鉴权：客户端用它做更新检查
    return {"ok": True, "version": APP_VERSION}


# ---- auth & users ----

def _issue_token(username: str, password: str, request: Request) -> dict:
    """校验账号密码并签发 JWT 对；带登录限流。成功/失败都抛/返一致，不区分用户名或密码错。"""
    ip = request.client.host if request.client else "-"
    key, fails = _login_throttle(username, ip)
    user = db.get_user_by_username(username)
    if not user or user["disabled"] or not verify_password(password, user["password_hash"]):
        fails += 1
        _login_fails[key] = (fails, time.monotonic() + 60 if fails >= 5 else 0.0)
        raise HTTPException(status_code=401, detail="wrong username or password")
    _login_fails.pop(key, None)
    return _token_pair(user)


def _token_pair(user: dict) -> dict:
    return {
        "access_token": make_token(user, "access"),
        "refresh_token": make_token(user, "refresh"),
        "token_type": "bearer",
        "expires_in": ACCESS_HOURS * 3600,
        "refresh_expires_in": REFRESH_DAYS * 86400,
        "user": public_user(user),
    }


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


@app.post("/api/v1/auth/refresh")
def refresh_token(payload: RefreshIn):
    """用 refresh token 换新的一对 token（无状态刷新，旧 refresh 在到期前仍可用）。
    客户端只保存 token、不保存密码；refresh 也失效时才需要重新输密码。"""
    user = _user_from_token(payload.refresh_token, kind="refresh")
    return _token_pair(user)


@app.get("/api/v1/auth/captcha")
def get_captcha():
    """图形验证码（登录用）。一次性、5 分钟过期。"""
    captcha_id, img = new_captcha()
    return {"captcha_id": captcha_id, "image": img}


@app.post("/api/v1/auth/login")
def login(payload: LoginIn, request: Request):
    """网页/管理后台登录：强制图形验证码。"""
    if not check_captcha(payload.captcha_id, payload.captcha_text):
        raise HTTPException(status_code=400, detail="captcha invalid or expired")
    return _issue_token(payload.username, payload.password, request)


@app.post("/api/v1/auth/token")
def machine_token(payload: LoginIn, request: Request):
    """机器客户端（agent / 桌面端等无人值守程序）登录：免验证码，仍受限流保护。"""
    return _issue_token(payload.username, payload.password, request)


@app.get("/api/v1/auth/me")
def me(user: dict = Depends(current_user)):
    return public_user(user)


@app.post("/api/v1/auth/change_password")
def change_password(payload: ChangePasswordIn, user: dict = Depends(current_user)):
    if not verify_password(payload.old_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="old password wrong")
    db.update_user(user["id"], password_hash=hash_password(payload.new_password))
    return {"ok": True}


@app.get("/api/v1/users")
def list_users(_: dict = Depends(require_admin)):
    return {"items": db.list_users()}


@app.post("/api/v1/users", status_code=201)
def create_user(payload: UserCreateIn, _: dict = Depends(require_admin)):
    if db.get_user_by_username(payload.username):
        raise HTTPException(status_code=409, detail="username exists")
    user = db.create_user(payload.username, hash_password(payload.password), payload.role)
    return public_user(user)


@app.patch("/api/v1/users/{user_id}")
def update_user(user_id: int, payload: UserUpdateIn, admin: dict = Depends(require_admin)):
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="not found")
    # 保护最后一个可用管理员
    demote = (payload.role == "user" and target["role"] == "admin") or \
             (payload.disabled and target["role"] == "admin" and not target["disabled"])
    if demote and db.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="cannot disable/demote the last admin")
    fields = {}
    if payload.password is not None:
        fields["password_hash"] = hash_password(payload.password)
    if payload.role is not None:
        fields["role"] = payload.role
    if payload.disabled is not None:
        fields["disabled"] = 1 if payload.disabled else 0
    return db.update_user(user_id, **fields)


@app.delete("/api/v1/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="not found")
    if target["role"] == "admin" and not target["disabled"] and db.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last admin")
    db.delete_user(user_id)
    return {"deleted": user_id}


@app.post("/api/v1/sms", status_code=201, dependencies=[Depends(current_user)])
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


@app.get("/api/v1/sms", dependencies=[Depends(current_user)])
def list_sms(
    limit: int = Query(default=50, ge=1, le=500),
    before_id: Optional[int] = None,
):
    return {"items": db.list_sms(limit=limit, before_id=before_id)}


@app.get("/api/v1/sms/{sms_id}", dependencies=[Depends(current_user)])
def get_sms(sms_id: int):
    row = db.get_sms(sms_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return row


@app.delete("/api/v1/sms/{sms_id}", dependencies=[Depends(current_user)])
async def delete_sms(sms_id: int):
    if not db.delete_sms(sms_id):
        raise HTTPException(status_code=404, detail="not found")
    await hub.broadcast(json.dumps({"type": "delete", "data": {"id": sms_id}}))
    return {"deleted": sms_id}


# ---- sms sending (downlink via agent) ----

@app.post("/api/v1/sms/send", dependencies=[Depends(current_user)])
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


@app.get("/api/v1/sms/outbox/list", dependencies=[Depends(current_user)])
def list_outbox(limit: int = Query(default=50, ge=1, le=500)):
    return {"items": db.list_outbox_sms(limit=limit)}


# ---- calls ----

@app.post("/api/v1/calls", status_code=201, dependencies=[Depends(current_user)])
async def report_call(event: CallEventIn):
    row = db.upsert_call(event.client_msg_id, **event.model_dump(exclude={"client_msg_id"}))
    await hub.broadcast(json.dumps({"type": "call", "data": row}, ensure_ascii=False))
    return row


@app.get("/api/v1/calls", dependencies=[Depends(current_user)])
def list_calls(limit: int = Query(default=50, ge=1, le=500)):
    return {"items": db.list_calls(limit=limit)}


@app.post("/api/v1/calls/dial", dependencies=[Depends(current_user)])
async def dial(payload: DialIn):
    try:
        ack = await agent_channel.command("dial", number=payload.number)
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "dial failed")
    return ack


@app.post("/api/v1/calls/answer", dependencies=[Depends(current_user)])
async def answer_call():
    try:
        ack = await agent_channel.command("answer")
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "answer failed")
    return ack


@app.post("/api/v1/calls/hangup", dependencies=[Depends(current_user)])
async def hangup_call():
    try:
        ack = await agent_channel.command("hangup")
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "hangup failed")
    return ack


@app.post("/api/v1/calls/{call_id}/recording", dependencies=[Depends(current_user)])
async def upload_recording(call_id: int, request: Request):
    if not db.get_call(call_id):
        raise HTTPException(status_code=404, detail="not found")
    if request.headers.get("content-length"):
        if int(request.headers["content-length"]) > MAX_RECORDING_BYTES:
            raise HTTPException(status_code=413, detail="recording too large")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    if len(data) > MAX_RECORDING_BYTES:
        raise HTTPException(status_code=413, detail="recording too large")
    filename = f"call_{call_id}.wav"
    (RECORDINGS_DIR / filename).write_bytes(data)
    row = db.set_recording(call_id, filename)
    await hub.broadcast(json.dumps({"type": "call", "data": row}, ensure_ascii=False))
    return {"ok": True, "size": len(data)}


@app.get("/api/v1/calls/{call_id}/recording", dependencies=[Depends(current_user)])
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
    try:
        payload = decode_token(token)
        if payload.get("typ", "access") != "access":
            raise ValueError("wrong token type")
        user = db.get_user(int(payload["sub"]))
        if not user or user["disabled"]:
            raise ValueError("user gone")
    except Exception:
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
    try:
        payload = decode_token(token)
        if payload.get("typ", "access") != "access":
            raise ValueError("wrong token type")
        user = db.get_user(int(payload["sub"]))
        if not user or user["disabled"]:
            raise ValueError("user gone")
    except Exception:
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

# 管理后台（admin/ 前端构建产物，hash 路由无需 fallback）
_admin_dir = BASE_DIR / "static" / "admin"
if _admin_dir.exists():
    app.mount("/admin", StaticFiles(directory=_admin_dir, html=True), name="admin")
