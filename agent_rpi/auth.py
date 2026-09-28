"""设备码认证与令牌管理：REST 上报和 WS 下行通道共用。

用 config 里的设备码（管理后台「设备管理」创建，smsk_ 开头）换 access+refresh
token 对；REST 遇 401 或 WS 被 4401 关闭时先用 refresh token 续期，refresh 也
失效才重新用设备码认证。线程安全（主线程与 uplink 线程都会用到）。
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("smsync.auth")


class JwtAuth:
    def __init__(self, base_url: str, device_key: str):
        self.base = base_url.rstrip("/")
        self.device_key = device_key
        self._access: str | None = None
        self._refresh: str | None = None
        self._lock = threading.Lock()

    def token(self) -> str:
        """取当前 access token，首次调用时用设备码认证。"""
        with self._lock:
            if self._access is None:
                self._device_login()
            return self._access

    def refresh(self) -> str:
        """令牌失效（REST 401 / WS 4401）时：先 refresh 续期，失败再重新设备码认证。"""
        with self._lock:
            if self._refresh:
                try:
                    self._post("/api/v1/auth/refresh", {"refresh_token": self._refresh})
                    return self._access
                except Exception as e:
                    log.warning("refresh 续期失败（%s），重新用设备码认证", e)
                    self._access = self._refresh = None
            self._device_login()
            return self._access

    # ---- 内部 ----

    def _post(self, path: str, body: dict):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read())
        self._access = data["access_token"]
        self._refresh = data["refresh_token"]

    def _device_login(self):
        """429（连续失败被锁定）时等 60 秒再试，不狂刷。"""
        while True:
            try:
                self._post("/api/v1/auth/device", {"device_key": self.device_key})
                log.info("设备已认证上线")
                return
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    log.warning("认证连续失败被锁定，60 秒后重试")
                    time.sleep(60)
                    continue
                if e.code == 401:
                    log.error("设备码无效或已被禁用，60 秒后重试（去管理后台检查设备）")
                    time.sleep(60)
                    continue
                raise RuntimeError(f"device login failed: HTTP {e.code}")
