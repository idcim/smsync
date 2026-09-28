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
    device_id: Optional[int] = None  # 多设备在线时指定目标设备


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
    device_id: Optional[int] = None  # 多设备在线时指定目标设备


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

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_subject(payload: dict) -> Optional[dict]:
    """把 JWT 的 sub 解析成用户/设备。设备 sub 形如 'dev:3'。无效返回 None。"""
    sub = str(payload.get("sub", ""))
    if sub.startswith("dev:"):
        try:
            dev = db.get_device(int(sub[4:]))
        except ValueError:
            return None
        if not dev or dev["disabled"]:
            return None
        return {"id": dev["id"], "username": dev["name"], "role": "device",
                "device": True, "owner_id": dev["owner_id"]}
    try:
        user = db.get_user(int(sub))
    except ValueError:
        return None
    if not user or user["disabled"]:
        return None
    return user


def _user_from_token(token: str, kind: str = "access") -> dict:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    # typ 缺省按 access 处理（兼容旧版签发的 token）
    if payload.get("typ", "access") != kind:
        raise HTTPException(status_code=401, detail="wrong token type")
    subject = _resolve_subject(payload)
    if not subject:
        raise HTTPException(status_code=401, detail="user/device disabled or deleted")
    return subject


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


# ---- 数据隔离：设备归属用户，用户只见自己设备的数据；admin 全量 ----

def visible_device_ids(subject: dict) -> Optional[set[int]]:
    """None = 全部可见（admin）；集合 = 只能看这些设备的数据（可能为空集）。"""
    if subject["role"] == "admin":
        return None
    if subject.get("device"):
        return {subject["id"]}
    return db.owned_device_ids(subject["id"])


def can_access_row(subject: dict, row: Optional[dict]) -> bool:
    """行级访问控制。device_id 为 NULL 的遗留数据只有 admin 可见。"""
    if not row:
        return False
    ids = visible_device_ids(subject)
    if ids is None:
        return True
    return row.get("device_id") in ids


def can_manage_device(subject: dict, dev: Optional[dict]) -> bool:
    """设备管理权限：admin 全部，用户只能管自己名下的设备。"""
    if not dev:
        return False
    return subject["role"] == "admin" or dev.get("owner_id") == subject["id"]


def resolve_command_device(subject: dict, device_id: Optional[int]) -> int:
    """确定下行指令的目标设备：显式指定则校验归属；未指定时若只有一台
    在线设备则自动用它，多在线报 400 要求指定。"""
    ids = visible_device_ids(subject)
    if device_id is not None:
        if ids is not None and device_id not in ids:
            raise HTTPException(status_code=403, detail="not your device")
        return device_id
    online = [d for d in agent_channel.online_ids() if ids is None or d in ids]
    if not online:
        raise HTTPException(status_code=503, detail="no device online")
    if len(online) > 1:
        raise HTTPException(status_code=400, detail="multiple devices online, specify device_id")
    return online[0]


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
    """客户端推送。每个连接记录其身份，广播按数据归属过滤：
    admin 全收；普通用户/设备只收自己设备的事件；无归属的遗留事件只发 admin。"""

    def __init__(self):
        self._clients: dict[WebSocket, dict] = {}  # ws -> subject
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket, subject: dict):
        async with self._lock:
            self._clients[ws] = subject

    async def remove(self, ws: WebSocket):
        async with self._lock:
            self._clients.pop(ws, None)

    async def broadcast(self, message: str, device_id: Optional[int] = None):
        async with self._lock:
            clients = list(self._clients.items())
        for ws, subject in clients:
            if subject["role"] == "admin":
                pass  # admin 全收
            elif device_id is not None and device_id in (visible_device_ids(subject) or set()):
                pass
            else:
                continue
            try:
                await ws.send_text(message)
            except Exception:
                with contextlib.suppress(Exception):
                    await self.remove(ws)


hub = WsHub()


class AgentOffline(Exception):
    pass


class AgentChannel:
    """采集端下行通道。每台设备一条持久 WS（/ws/agent），指令按设备路由，
    回答用 ack id 对应。"""

    def __init__(self):
        self.channels: dict[int, WebSocket] = {}          # device_id -> ws
        self.pending: dict[str, tuple[int, asyncio.Future]] = {}

    def online_ids(self) -> list[int]:
        return list(self.channels.keys())

    async def attach(self, ws: WebSocket, device_id: int):
        old = self.channels.get(device_id)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close(code=4000)
        self.channels[device_id] = ws

    def detach(self, ws: WebSocket, device_id: int):
        if self.channels.get(device_id) is ws:
            self.channels.pop(device_id, None)
            for cmd_id, (dev_id, fut) in list(self.pending.items()):
                if dev_id == device_id and not fut.done():
                    fut.set_exception(AgentOffline("agent disconnected"))
                    self.pending.pop(cmd_id, None)

    async def command(self, device_id: int, action: str, timeout: float = 20.0, **params) -> dict:
        ws = self.channels.get(device_id)
        if ws is None:
            raise AgentOffline()
        cmd_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending[cmd_id] = (device_id, fut)
        try:
            await ws.send_text(json.dumps(
                {"id": cmd_id, "action": action, **params}, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(cmd_id, None)

    def handle_message(self, data: dict):
        ack_id = data.get("ack")
        if ack_id and ack_id in self.pending:
            _, fut = self.pending[ack_id]
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


class DeviceLoginIn(BaseModel):
    device_key: str = Field(min_length=1, max_length=128)


class DeviceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    owner_id: Optional[int] = None  # 仅 admin 可指定他人；省略则归属自己


class DeviceUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    disabled: Optional[bool] = None
    owner_id: Optional[int] = None  # 仅 admin 可改（显式传 null 解绑）


def _device_key_hash(device_key: str) -> str:
    # 设备码是高熵随机串，sha256 足够（无需 PBKDF2）
    import hashlib as _hl
    return _hl.sha256(device_key.encode("utf-8")).hexdigest()


@app.post("/api/v1/auth/refresh")
def refresh_token(payload: RefreshIn):
    """用 refresh token 换新的一对 token（无状态刷新，旧 refresh 在到期前仍可用）。
    客户端只保存 token、不保存密码；refresh 也失效时才需要重新输密码。"""
    user = _user_from_token(payload.refresh_token, kind="refresh")
    if user.get("device"):
        db.update_device(user["id"], last_seen_at=_now_iso())
    return _token_pair(user)


@app.post("/api/v1/auth/device")
def device_login(payload: DeviceLoginIn, request: Request):
    """采集端用设备码换 token 对（免验证码，受限流保护）。"""
    ip = request.client.host if request.client else "-"
    key, fails = _login_throttle(payload.device_key[:16], ip)
    dev = db.get_device_by_hash(_device_key_hash(payload.device_key))
    if not dev or dev["disabled"]:
        fails += 1
        _login_fails[key] = (fails, time.monotonic() + 60 if fails >= 5 else 0.0)
        raise HTTPException(status_code=401, detail="invalid device key")
    _login_fails.pop(key, None)
    db.clear_device_key(dev["id"])  # 已使用：清除明文设备码，后台不再可见
    db.update_device(dev["id"], last_seen_at=_now_iso())
    return _token_pair({
        "id": dev["id"], "username": dev["name"], "role": "device", "device": True,
    })


# ---- 设备管理（admin 管全部；普通用户管自己名下的） ----

@app.get("/api/v1/devices")
def list_devices(user: dict = Depends(current_user)):
    online = set(agent_channel.online_ids())
    if user["role"] == "admin":
        items = db.list_devices()
    elif user.get("device"):
        items = []  # 设备身份不需要管理设备
    else:
        items = db.list_devices(owner_id=user["id"])
    # online 是实时状态（/ws/agent 指令通道是否连着），与 last_seen_at 不同
    for it in items:
        it["online"] = it["id"] in online
    return {"items": items}


@app.post("/api/v1/devices", status_code=201)
def create_device(payload: DeviceCreateIn, user: dict = Depends(current_user)):
    if user.get("device"):
        raise HTTPException(status_code=403, detail="device cannot create devices")
    owner_id = user["id"]
    if payload.owner_id is not None:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="only admin can assign owner")
        if not db.get_user(payload.owner_id):
            raise HTTPException(status_code=400, detail="owner user not found")
        owner_id = payload.owner_id
    # 设备码在设备首次上线前可在列表中查看；设备认证成功后服务端清除明文
    device_key = "smsk_" + secrets.token_urlsafe(24)
    return db.create_device(payload.name, _device_key_hash(device_key), device_key, owner_id)


@app.patch("/api/v1/devices/{device_id}")
def update_device(device_id: int, payload: DeviceUpdateIn, user: dict = Depends(current_user)):
    dev = db.get_device(device_id)
    if not dev or not can_manage_device(user, dev):
        raise HTTPException(status_code=404, detail="not found")
    fields = {}
    if payload.name is not None:
        fields["name"] = payload.name
    if payload.disabled is not None:
        fields["disabled"] = 1 if payload.disabled else 0
    if "owner_id" in payload.model_fields_set:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="only admin can reassign owner")
        if payload.owner_id is not None and not db.get_user(payload.owner_id):
            raise HTTPException(status_code=400, detail="owner user not found")
        fields["owner_id"] = payload.owner_id
    return db.update_device(device_id, **fields)


@app.post("/api/v1/devices/{device_id}/regenerate")
def regenerate_device_key(device_id: int, user: dict = Depends(current_user)):
    """重置设备码：旧码立即失效，返回新码（同样只在设备下次使用前可见）。
    用于设备码丢失（如创建弹窗被误关）时的找回。"""
    dev = db.get_device(device_id)
    if not dev or not can_manage_device(user, dev):
        raise HTTPException(status_code=404, detail="not found")
    device_key = "smsk_" + secrets.token_urlsafe(24)
    return db.regenerate_device_key(device_id, _device_key_hash(device_key), device_key)


@app.delete("/api/v1/devices/{device_id}")
def delete_device(device_id: int, user: dict = Depends(current_user)):
    dev = db.get_device(device_id)
    if not dev or not can_manage_device(user, dev):
        raise HTTPException(status_code=404, detail="not found")
    db.delete_device(device_id)
    return {"deleted": device_id}


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


@app.post("/api/v1/sms", status_code=201)
async def create_sms(payload: SmsIn, user: dict = Depends(current_user)):
    # 设备上报时打上设备归属；用户手动上报（不常见）无归属，仅 admin 可见
    device_id = user["id"] if user.get("device") else None
    row, created = db.insert_sms(
        sender=payload.sender,
        text=payload.text,
        received_at=payload.received_at,
        client_msg_id=payload.client_msg_id,
        device_id=device_id,
    )
    if created:
        await hub.broadcast(to_event(row), device_id=row["device_id"])
    return row


@app.get("/api/v1/sms")
def list_sms(
    limit: int = Query(default=50, ge=1, le=500),
    before_id: Optional[int] = None,
    user: dict = Depends(current_user),
):
    return {"items": db.list_sms(limit=limit, before_id=before_id,
                                 device_ids=visible_device_ids(user))}


@app.get("/api/v1/sms/{sms_id}")
def get_sms(sms_id: int, user: dict = Depends(current_user)):
    row = db.get_sms(sms_id)
    if not can_access_row(user, row):
        raise HTTPException(status_code=404, detail="not found")
    return row


@app.delete("/api/v1/sms/{sms_id}")
async def delete_sms(sms_id: int, user: dict = Depends(current_user)):
    row = db.get_sms(sms_id)
    if not can_access_row(user, row):
        raise HTTPException(status_code=404, detail="not found")
    db.delete_sms(sms_id)
    await hub.broadcast(json.dumps({"type": "delete", "data": {"id": sms_id}}),
                        device_id=row["device_id"])
    return {"deleted": sms_id}


# ---- sms sending (downlink via agent) ----

@app.post("/api/v1/sms/send")
async def send_sms(payload: SmsSendIn, user: dict = Depends(current_user)):
    # 先落发件箱再路由：即使设备不在线/需指定设备，失败也要留痕
    row = db.insert_outbox_sms(uuid.uuid4().hex, payload.to, payload.text, payload.device_id)
    try:
        device_id = resolve_command_device(user, payload.device_id)
    except HTTPException as e:
        row = db.update_outbox_status(row["client_msg_id"], "failed", e.detail)
        await hub.broadcast(json.dumps({"type": "sms_sent", "data": row}, ensure_ascii=False),
                            device_id=payload.device_id)
        raise
    if device_id != row.get("device_id"):
        # 自动路由到的设备写入行（广播过滤用）
        row["device_id"] = device_id
    try:
        ack = await agent_channel.command(
            device_id, "send_sms", to=payload.to, text=payload.text,
            client_msg_id=row["client_msg_id"])
        ok, error = bool(ack.get("ok")), ack.get("error")
    except AgentOffline:
        ok, error = False, "agent offline"
    row = db.update_outbox_status(row["client_msg_id"], "sent" if ok else "failed", error)
    row["device_id"] = device_id
    await hub.broadcast(json.dumps({"type": "sms_sent", "data": row}, ensure_ascii=False),
                        device_id=device_id)
    if not ok:
        raise HTTPException(status_code=503 if error == "agent offline" else 502,
                            detail=error or "send failed")
    return row


@app.get("/api/v1/sms/outbox/list")
def list_outbox(limit: int = Query(default=50, ge=1, le=500),
                user: dict = Depends(current_user)):
    return {"items": db.list_outbox_sms(limit=limit, device_ids=visible_device_ids(user))}


# ---- calls ----

@app.post("/api/v1/calls", status_code=201)
async def report_call(event: CallEventIn, user: dict = Depends(current_user)):
    fields = event.model_dump(exclude={"client_msg_id"})
    if user.get("device"):
        fields["device_id"] = user["id"]
    row = db.upsert_call(event.client_msg_id, **fields)
    await hub.broadcast(json.dumps({"type": "call", "data": row}, ensure_ascii=False),
                        device_id=row["device_id"])
    return row


@app.get("/api/v1/calls")
def list_calls(limit: int = Query(default=50, ge=1, le=500),
               user: dict = Depends(current_user)):
    return {"items": db.list_calls(limit=limit, device_ids=visible_device_ids(user))}


class DeviceTargetIn(BaseModel):
    device_id: Optional[int] = None


@app.post("/api/v1/calls/dial")
async def dial(payload: DialIn, user: dict = Depends(current_user)):
    device_id = resolve_command_device(user, payload.device_id)
    try:
        ack = await agent_channel.command(device_id, "dial", number=payload.number)
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "dial failed")
    return ack


@app.post("/api/v1/calls/answer")
async def answer_call(payload: Optional[DeviceTargetIn] = None,
                      user: dict = Depends(current_user)):
    device_id = resolve_command_device(user, payload.device_id if payload else None)
    try:
        ack = await agent_channel.command(device_id, "answer")
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "answer failed")
    return ack


@app.post("/api/v1/calls/hangup")
async def hangup_call(payload: Optional[DeviceTargetIn] = None,
                      user: dict = Depends(current_user)):
    device_id = resolve_command_device(user, payload.device_id if payload else None)
    try:
        ack = await agent_channel.command(device_id, "hangup")
    except AgentOffline:
        raise HTTPException(status_code=503, detail="agent offline")
    if not ack.get("ok"):
        raise HTTPException(status_code=502, detail=ack.get("error") or "hangup failed")
    return ack


@app.post("/api/v1/calls/{call_id}/recording")
async def upload_recording(call_id: int, request: Request, user: dict = Depends(current_user)):
    call = db.get_call(call_id)
    if not can_access_row(user, call):
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
    await hub.broadcast(json.dumps({"type": "call", "data": row}, ensure_ascii=False),
                        device_id=row["device_id"])
    return {"ok": True, "size": len(data)}


@app.get("/api/v1/calls/{call_id}/recording")
def get_recording(call_id: int, user: dict = Depends(current_user)):
    call = db.get_call(call_id)
    if not can_access_row(user, call) or not call.get("recording_file"):
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
        subject = _resolve_subject(payload)
        if not subject:
            raise ValueError("user/device gone")
    except Exception:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await hub.add(websocket, subject)
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
        subject = _resolve_subject(payload)
        if not subject:
            raise ValueError("user/device gone")
    except Exception:
        await websocket.close(code=4401)
        return
    if not subject.get("device"):
        # 指令通道只允许设备身份接入
        await websocket.close(code=4403)
        return
    device_id = subject["id"]
    await websocket.accept()
    await agent_channel.attach(websocket, device_id)
    db.update_device(device_id, last_seen_at=_now_iso())  # 指令通道建立也算一次上线
    await hub.broadcast(json.dumps({"type": "agent", "data": {
        "online": True, "device_id": device_id, "device_name": subject["username"],
    }}, ensure_ascii=False), device_id=device_id)
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
        agent_channel.detach(websocket, device_id)
        await hub.broadcast(json.dumps({"type": "agent", "data": {
            "online": False, "device_id": device_id, "device_name": subject["username"],
        }}, ensure_ascii=False), device_id=device_id)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# 管理后台（admin/ 前端构建产物，hash 路由无需 fallback）
_admin_dir = BASE_DIR / "static" / "admin"
if _admin_dir.exists():
    app.mount("/admin", StaticFiles(directory=_admin_dir, html=True), name="admin")
