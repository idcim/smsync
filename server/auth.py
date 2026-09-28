"""用户认证：PBKDF2 密码哈希 + JWT 签发/校验。

密钥优先级：环境变量 SMSYNC_JWT_SECRET > server/.jwt_secret（首次自动生成）。
token 有效期由 SMSYNC_JWT_EXPIRE_HOURS 控制（默认 7 天）。
"""

import hashlib
import hmac
import io
import base64
import os
import random
import secrets
import threading
import time

import jwt
from PIL import Image, ImageDraw, ImageFont

from config import DATA_DIR

_PBKDF2_ITER = 100_000
ALGORITHM = "HS256"
# access token 短效（默认 12 小时），refresh token 长效（默认 30 天）；
# 客户端只保存 token，access 过期后用 refresh 换新的一对（无状态）
ACCESS_HOURS = int(os.environ.get("SMSYNC_JWT_ACCESS_HOURS", "12"))
REFRESH_DAYS = int(os.environ.get("SMSYNC_JWT_REFRESH_DAYS", "30"))
EXPIRE_HOURS = ACCESS_HOURS  # 兼容旧引用

# 放数据目录而非代码目录：Docker 里代码目录是只读 rootfs，数据目录在卷上
_secret_file = DATA_DIR / ".jwt_secret"


def _get_secret() -> str:
    secret = os.environ.get("SMSYNC_JWT_SECRET")
    if secret:
        return secret
    if _secret_file.exists():
        return _secret_file.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(32)
    _secret_file.write_text(secret, encoding="utf-8")
    return secret


SECRET = _get_secret()


# ---- 密码哈希（PBKDF2-HMAC-SHA256，标准库实现，无额外依赖） ----

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iter_s, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iter_s)
        ).hex()
        return hmac.compare_digest(calc, digest)
    except (ValueError, AttributeError):
        return False


# ---- JWT ----

def make_token(user: dict, kind: str = "access") -> str:
    """kind: "access"（业务请求用，短效） | "refresh"（仅用于换新 token，长效）。
    设备身份的 sub 带 dev: 前缀，与用户名账号区分。"""
    now = int(time.time())
    ttl = ACCESS_HOURS * 3600 if kind == "access" else REFRESH_DAYS * 86400
    sub = f"dev:{user['id']}" if user.get("device") else str(user["id"])
    payload = {
        "sub": sub,
        "username": user["username"],
        "role": user["role"],
        "typ": kind,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """校验并返回 payload；无效/过期抛 jwt 异常。"""
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])


# ---- 图形验证码（登录防爆破；一次性，5 分钟过期，内存存储） ----

_CAPTCHA_TTL = 300
_CAPTCHA_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # 去掉易混淆的 0/O/1/I
_captchas: dict[str, tuple[str, float]] = {}
_captchas_lock = threading.Lock()


def new_captcha() -> tuple[str, str]:
    """生成验证码，返回 (captcha_id, data:image/png;base64 URL)。"""
    code = "".join(random.choice(_CAPTCHA_CHARS) for _ in range(4))
    captcha_id = secrets.token_urlsafe(12)

    w, h = 132, 44
    img = Image.new("RGB", (w, h), (13, 17, 23))
    draw = ImageDraw.Draw(img)
    # 干扰线
    for _ in range(4):
        draw.line(
            [(random.randint(0, w), random.randint(0, h)) for _ in range(2)],
            fill=(random.randint(60, 120),) * 3, width=1)
    # 字符（随机颜色/位置/字号）
    for i, ch in enumerate(code):
        font = ImageFont.load_default(size=random.randint(24, 30))
        color = (random.randint(150, 255), random.randint(150, 255), random.randint(150, 255))
        draw.text((10 + i * 30, random.randint(4, 12)), ch, font=font, fill=color)
    # 噪点
    for _ in range(120):
        draw.point((random.randint(0, w - 1), random.randint(0, h - 1)),
                   fill=(random.randint(80, 160),) * 3)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    with _captchas_lock:
        # 顺手清理过期项
        now = time.monotonic()
        for k in [k for k, (_, exp) in _captchas.items() if exp < now]:
            _captchas.pop(k, None)
        _captchas[captcha_id] = (code.lower(), now + _CAPTCHA_TTL)
    return captcha_id, data_url


def check_captcha(captcha_id: str, text: str) -> bool:
    """校验并销毁（一次性）。大小写不敏感。"""
    with _captchas_lock:
        item = _captchas.pop(captcha_id or "", None)
    if not item:
        return False
    code, exp = item
    return exp >= time.monotonic() and (text or "").strip().lower() == code
