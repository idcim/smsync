"""SMSync agent：EC20 短信采集上传 + 系统托盘 GUI。

Run:  python agent.py   （打包后：smsync-agent.exe，驻留系统托盘）
Conf: 读取顺序 %APPDATA%\\SMSyncAgent\\config.ini → exe 旁 config.ini → 内置 config.example.ini；
      设置窗口保存永远写 %APPDATA% 那份（Program Files 下 exe 目录不可写）。
依赖：pyserial、websocket-client、pystray、Pillow（tkinter 为标准库）。
"""

import configparser
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from tkinter import ttk

import pystray
import serial
from PIL import Image, ImageDraw

from modem import Modem
from outbox import Outbox
from uplink import Uplink

__version__ = "1.3.0"

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：数据与配置放 %APPDATA%（Program Files 不可写）
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    DATA_DIR = Path(os.environ.get("APPDATA", str(BASE_DIR))) / "SMSyncAgent"
else:
    BASE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = BASE_DIR
    DATA_DIR = BASE_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 设置窗口保存的目标配置（冻结模式下在 %APPDATA%，开发模式即 agent/config.ini）
CONFIG_PATH = DATA_DIR / "config.ini"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / "agent.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("smsync.agent")


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for path in (CONFIG_PATH, BASE_DIR / "config.ini", BUNDLE_DIR / "config.example.ini"):
        if path.exists():
            cfg.read(path, encoding="utf-8")
            log.info("config loaded from %s", path)
            return cfg
    sys.exit("no config.ini / config.example.ini found")


def save_config(server_url: str, device_key: str, port: str):
    """设置窗口保存：只覆盖 url/device_key/port 三项，其余配置（[agent] 等）保留。"""
    cfg = load_config()
    for section in ("server", "modem"):
        if not cfg.has_section(section):
            cfg.add_section(section)
    cfg.set("server", "url", server_url)
    cfg.set("server", "device_key", device_key)
    cfg.set("modem", "port", port)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)
    log.info("config saved to %s", CONFIG_PATH)


# ---- 运行状态（采集线程写，GUI 读） ----------------------------------------

_status_lock = threading.Lock()
_status_text = "未配置"


def set_status(text: str):
    global _status_text
    with _status_lock:
        _status_text = text
    log.info("status: %s", text)


def get_status() -> str:
    with _status_lock:
        return _status_text


class Uploader:
    """设备码认证：/auth/device 换 token 对；REST 401 先 refresh 续期，失败再重新认证。"""

    def __init__(self, base_url: str, device_key: str, stop: threading.Event, on_status):
        self.base = base_url.rstrip("/")
        self.url = self.base + "/api/v1/sms"
        self.device_key = device_key
        self._stop = stop
        self._status = on_status
        self.token: str | None = None          # access token
        self.refresh_token: str | None = None
        self._token_lock = threading.Lock()    # token 读写（REST 线程与 uplink 线程共享）
        self._auth_lock = threading.Lock()     # 串行化 login/refresh，避免并发重复认证

    def _auth(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return json.loads(resp.read())

    def _save_tokens(self, data: dict):
        with self._token_lock:
            self.token = data["access_token"]
            self.refresh_token = data.get("refresh_token")

    def access_token(self) -> str:
        """取当前 access token（没有则先设备码认证）；REST 与 WS uplink 共用。"""
        with self._token_lock:
            token = self.token
        if token is None:
            self.login()
            with self._token_lock:
                token = self.token
        return token

    def login(self):
        """设备码认证；429 限流 / 401 设备码无效都等 60 秒再试（等待可被停止打断）。"""
        with self._auth_lock:
            while not self._stop.is_set():
                try:
                    self._save_tokens(self._auth("/api/v1/auth/device", {"device_key": self.device_key}))
                    log.info("设备认证成功")
                    self._status("认证成功")
                    return
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        log.warning("认证被限流，60 秒后重试")
                        self._status("认证被限流，60 秒后重试")
                    elif e.code == 401:
                        # 设备码错误或设备被禁用：立即重试没意义，等用户在设置里改
                        log.error("设备码无效或设备已禁用，60 秒后重试")
                        self._status("错误：设备码无效或已禁用")
                    else:
                        raise RuntimeError(f"device auth failed: HTTP {e.code}")
                self._stop.wait(60)
            raise RuntimeError("agent stopped")

    def refresh(self) -> bool:
        """refresh_token 无状态续期；失败返回 False（调用方再重新认证）。"""
        with self._auth_lock:
            with self._token_lock:
                refresh_token = self.refresh_token
            if not refresh_token:
                return False
            try:
                self._save_tokens(self._auth("/api/v1/auth/refresh", {"refresh_token": refresh_token}))
                log.info("access token 已续期")
                return True
            except Exception as e:
                log.warning("token refresh failed: %s", e)
                return False

    def _post(self, payload: dict, timeout: float):
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token()}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"server returned HTTP {resp.status}")

    def send(self, payload: dict, timeout: float = 10.0):
        try:
            self._post(payload, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            # access token 过期：先 refresh 续期重试一次，refresh 失败才用设备码重新认证
            log.info("JWT 失效，尝试 refresh 续期")
            if not self.refresh():
                self.login()
            self._post(payload, timeout)


class Agent:
    def __init__(self, cfg: configparser.ConfigParser, on_status=set_status):
        self._status = on_status
        self._stop = threading.Event()
        self.modem = Modem(
            cfg.get("modem", "port", fallback="COM9"),
            cfg.getint("modem", "baudrate", fallback=115200),
        )
        # 采集主循环与 uplink 指令线程共用串口，所有 modem 操作都要持锁
        self.modem_lock = threading.Lock()
        self.uploader = Uploader(
            cfg.get("server", "url", fallback="http://127.0.0.1:8000"),
            cfg.get("server", "device_key", fallback=""),
            self._stop,
            on_status,
        )
        self.uplink = Uplink(self.uploader, self.execute_command, self._stop, on_status)
        outbox_name = cfg.get("agent", "outbox_db", fallback="outbox.db")
        outbox_path = Path(outbox_name)
        if not outbox_path.is_absolute():
            outbox_path = DATA_DIR / outbox_name
        self.outbox = Outbox(str(outbox_path))
        self.heartbeat_sec = cfg.getint("agent", "heartbeat_sec", fallback=30)
        self.flush_sec = cfg.getint("agent", "flush_sec", fallback=10)
        self._last_heartbeat = 0.0
        self._last_flush = 0.0

    def stop(self):
        """让主循环和 uplink 尽快退出（关串口/关 WS 打断阻塞，各处 wait 被唤醒）。"""
        self._stop.set()
        self.modem.close()
        self.uplink.stop()

    # ---- downlink commands -------------------------------------------------

    def execute_command(self, action: str, params: dict) -> tuple[bool, str]:
        if action == "send_sms":
            with self.modem_lock:
                ok, info = self.modem.send_sms(params["to"], params["text"])
            return ok, "" if ok else info
        if action in ("dial", "answer", "hangup"):
            # Windows 采集端不支持通话控制
            return False, "unsupported on windows agent"
        return False, f"unknown action {action!r}"

    # ---- modem lifecycle -------------------------------------------------

    def connect(self):
        while not self._stop.is_set():
            try:
                with self.modem_lock:
                    self.modem.open()
                    self.modem.init_basic()
                return
            except Exception as e:
                log.error("modem connect failed (%s); retrying in 5s", e)
                self.modem.close()
                self._stop.wait(5)

    def wait_for_sim(self):
        while not self._stop.is_set():
            try:
                with self.modem_lock:
                    ready = self.modem.sim_ready()
                if ready:
                    log.info("SIM ready")
                    return
            except Exception as e:
                log.error("CPIN check failed: %s", e)
                raise
            log.warning("SIM not ready, waiting 10s...")
            self._stop.wait(10)

    # ---- sms handling ----------------------------------------------------

    def handle_sms(self, sms: dict):
        payload = {
            "sender": sms.get("sender") or "unknown",
            "text": sms.get("text") or "",
            "received_at": sms.get("received_at"),
            "client_msg_id": uuid.uuid4().hex,
        }
        log.info("SMS from %s: %.40s", payload["sender"], payload["text"])
        try:
            self.uploader.send(payload)
            log.info("uploaded")
        except Exception as e:
            log.warning("upload failed (%s); queued locally", e)
            self.outbox.enqueue(payload["client_msg_id"], payload)

    def process_index(self, index: int):
        with self.modem_lock:
            sms = self.modem.read_sms(index)
            self.modem.delete_sms(index)
        # Always delete: unparseable messages must not jam the SIM storage.
        if sms:
            self.handle_sms(sms)

    def drain_stored(self):
        with self.modem_lock:
            stored = self.modem.list_all()
        if stored:
            log.info("draining %d stored message(s)", len(stored))
        for index, sms in stored:
            self.handle_sms(sms)
            with self.modem_lock:
                self.modem.delete_sms(index)

    def flush_outbox(self):
        due = self.outbox.due()
        for row_id, payload, attempts in due:
            try:
                self.uploader.send(payload)
                self.outbox.done(row_id)
                log.info("re-sent queued SMS %s", payload["client_msg_id"][:8])
            except Exception as e:
                self.outbox.fail(row_id, attempts)
                log.warning("retry failed (%s), %d still queued", e, self.outbox.size())

    # ---- main loop -------------------------------------------------------

    def run(self):
        while not self._stop.is_set():
            try:
                self.connect()
                if self._stop.is_set():
                    break
                self.wait_for_sim()
                if self._stop.is_set():
                    break
                with self.modem_lock:
                    self.modem.init_sms()
                self.drain_stored()
                self.loop()
            except (serial.SerialException, OSError) as e:
                if self._stop.is_set():
                    break
                log.error("serial error (%s); reconnecting in 5s", e)
                self._status("错误：串口断开，重连中")
                self.modem.close()
                self._stop.wait(5)
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("unexpected error; restarting in 5s")
                self.modem.close()
                self._stop.wait(5)
        self.modem.close()
        log.info("agent loop stopped")

    def loop(self):
        log.info("listening for incoming SMS...")
        self._status("已上线")
        while not self._stop.is_set():
            with self.modem_lock:
                urc = self.modem.poll_urc(timeout=1.0)
            now = time.monotonic()
            if urc:
                index = Modem.cmti_index(urc)
                if index is not None:
                    self.process_index(index)
                elif urc.startswith(("+CMT", "^")):
                    log.info("URC: %s", urc)
            if now - self._last_heartbeat >= self.heartbeat_sec:
                self._last_heartbeat = now
                with self.modem_lock:
                    log.info("heartbeat: %s", self.modem.signal())
            if now - self._last_flush >= self.flush_sec:
                self._last_flush = now
                self.flush_outbox()


# ---- 采集线程管理 ----------------------------------------------------------

class AgentRunner:
    """启动 / 停止 / 保存配置后重启采集线程（不重启进程）。"""

    def __init__(self):
        self._agent: Agent | None = None
        self._thread: threading.Thread | None = None

    def start(self, cfg: configparser.ConfigParser):
        self.stop()
        if not cfg.get("server", "device_key", fallback="").strip():
            set_status("未配置（请填写设备码）")
            return
        set_status("连接中")
        self._agent = Agent(cfg)
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent")
        self._thread.start()
        self._agent.uplink.start()  # 指令通道独立于串口重连，随 Agent 同生命周期

    def _run(self):
        try:
            self._agent.run()
        except Exception:
            log.exception("agent thread crashed")
            set_status("错误：采集线程异常退出")

    def stop(self):
        if self._agent is not None:
            self._agent.stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                log.warning("agent thread did not stop in time")
        self._agent = None
        self._thread = None


# ---- 托盘图标 --------------------------------------------------------------

def make_icon_image(color: str = "#1e88e5") -> Image.Image:
    """现场画一个纯色圆形图标，不引入图片资源文件。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=color)
    return img


def run_tray(cmd_queue: queue.Queue, icon_holder: dict):
    """pystray 独占一个线程跑消息循环；菜单动作经队列转交 tkinter 主线程。"""

    def on_open(icon, item):
        cmd_queue.put("open")

    def on_quit(icon, item):
        cmd_queue.put("quit")

    icon = pystray.Icon(
        "smsync-agent",
        make_icon_image(),
        f"SMSync Agent v{__version__}",
        pystray.Menu(
            pystray.MenuItem("打开设置", on_open, default=True),
            pystray.MenuItem("退出", on_quit),
        ),
    )
    icon_holder["icon"] = icon
    icon.run()


# ---- 设置窗口 --------------------------------------------------------------

class SettingsWindow:
    """tkinter 设置窗口：服务器地址 / 设备码 / 串口 + 保存 + 状态标签。"""

    def __init__(self, root: tk.Tk, on_save, get_status_fn):
        self.root = root
        self.on_save = on_save            # 保存后回调：重启采集线程
        self.get_status = get_status_fn
        self.win: tk.Toplevel | None = None

    def open(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.lift()
            self.win.focus_force()
            return
        cfg = load_config()
        win = tk.Toplevel(self.root)
        win.title(f"SMSync Agent v{__version__} 设置")
        win.resizable(False, False)

        url_var = tk.StringVar(value=cfg.get("server", "url", fallback="http://127.0.0.1:8000"))
        key_var = tk.StringVar(value=cfg.get("server", "device_key", fallback=""))
        port_var = tk.StringVar(value=cfg.get("modem", "port", fallback="COM9"))

        form = ttk.Frame(win, padding=12)
        form.grid()
        for i, (label, var) in enumerate((("服务器地址", url_var), ("设备码", key_var), ("串口", port_var))):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=4)
            ttk.Entry(form, textvariable=var, width=42).grid(row=i, column=1, pady=4)
        ttk.Label(form, text="设备码在管理后台「设备管理」中创建设备获得（smsk_ 开头）",
                  foreground="#888888").grid(row=3, column=0, columnspan=2, sticky="w")

        status_var = tk.StringVar(value=self.get_status())
        ttk.Label(form, textvariable=status_var, foreground="#1e88e5").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def save():
            try:
                save_config(url_var.get().strip(), key_var.get().strip(), port_var.get().strip())
            except Exception as e:
                log.exception("save config failed")
                status_var.set(f"保存失败：{e}")
                return
            status_var.set("已保存，正在重启采集…")
            self.on_save()

        ttk.Button(form, text="保存", command=save).grid(row=5, column=1, sticky="e", pady=(8, 0))
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self.win = win

        def poll_status():
            if self.win is None or not self.win.winfo_exists():
                return
            status_var.set(self.get_status())
            self.win.after(1000, poll_status)

        poll_status()


def main():
    log.info("smsync-agent v%s starting (data dir: %s)", __version__, DATA_DIR)
    cmd_queue: queue.Queue = queue.Queue()
    icon_holder: dict = {}
    runner = AgentRunner()

    # tkinter 主循环跑主线程（隐藏主窗口，只留托盘 + 设置窗口）
    root = tk.Tk()
    root.withdraw()
    settings = SettingsWindow(root, on_save=lambda: runner.start(load_config()), get_status_fn=get_status)

    threading.Thread(target=run_tray, args=(cmd_queue, icon_holder), daemon=True, name="tray").start()

    def poll_queue():
        try:
            while True:
                cmd = cmd_queue.get_nowait()
                if cmd == "open":
                    settings.open()
                elif cmd == "quit":
                    root.quit()
                    return
        except queue.Empty:
            pass
        root.after(200, poll_queue)

    root.after(200, poll_queue)

    cfg = load_config()
    runner.start(cfg)
    if not cfg.get("server", "device_key", fallback="").strip():
        # 首次运行（没有设备码）：自动弹出设置窗口
        root.after(300, settings.open)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    log.info("shutting down...")
    runner.stop()
    icon = icon_holder.get("icon")
    if icon is not None:
        icon.stop()
    root.destroy()


if __name__ == "__main__":
    main()
