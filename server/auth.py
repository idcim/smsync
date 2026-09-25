"""用户认证：PBKDF2 密码哈希 + JWT 签发/校验。

密钥优先级：环境变量 SMSYNC_JWT_SECRET > server/.jwt_secret（首次自动生成）。
token 有效期由 SMSYNC_JWT_EXPIRE_HOURS 控制（默认 7 天）。
"""

import hashlib
import hmac
import os
import secrets
import time

import jwt

from config import DATA_DIR

_PBKDF2_ITER = 100_000
ALGORITHM = "HS256"
EXPIRE_HOURS = int(os.environ.get("SMSYNC_JWT_EXPIRE_HOURS", str(24 * 7)))

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

def make_token(user: dict) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user["role"],
        "iat": now,
        "exp": now + EXPIRE_HOURS * 3600,
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """校验并返回 payload；无效/过期抛 jwt 异常。"""
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])
