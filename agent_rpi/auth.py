"""JWT 登录与令牌管理：REST 上报和 WS 下行通道共用。

用 config 里的用户名密码登录换 JWT；REST 遇 401 或 WS 被 4401 关闭时
调用 refresh() 重新登录。线程安全（主线程与 uplink 线程都会用到）。
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("smsync.auth")


class JwtAuth:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._token: str | None = None
        self._lock = threading.Lock()

    def token(self) -> str:
        """取当前 JWT，首次调用时登录。"""
        with self._lock:
            if self._token is None:
                self._login()
            return self._token

    def refresh(self) -> str:
        """令牌失效（REST 401 / WS 4401）时强制重新登录。"""
        with self._lock:
            self._login()
            return self._token

    def _login(self):
        """429（连续失败被锁定）时等 60 秒再试，不狂刷。"""
        while True:
            req = urllib.request.Request(
                self.base + "/api/v1/auth/token",
                data=json.dumps({"username": self.username, "password": self.password}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    data = json.loads(resp.read())
                self._token = data["access_token"]
                log.info("已登录为 %s", data.get("user", {}).get("username", self.username))
                return
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    log.warning("登录连续失败被锁定，60 秒后重试")
                    time.sleep(60)
                    continue
                raise RuntimeError(f"login failed: HTTP {e.code}")
