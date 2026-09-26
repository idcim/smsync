"use strict";

const fs = require("fs");
const path = require("path");
const {
  app,
  BrowserWindow,
  clipboard,
  ipcMain,
  nativeImage,
  screen,
  shell,
  Tray,
  Menu,
} = require("electron");
const WebSocket = require("ws");

const base = require("./config");

// ---------------------------------------------------------------- settings

const DEFAULT_SETTINGS = {
  username: "", // empty -> fall back to env / .env
  refreshToken: "", // 登录换取的刷新令牌；access token 过期后用它无状态续期
  domains: "", // comma separated, empty -> env / built-in defaults
  popupEnabled: true,
  soundEnabled: true,
  autoStart: false,
};
let settings = { ...DEFAULT_SETTINGS };
let effToken = null; // 登录后拿到的 access token（JWT），只在内存不持久化
let effUsername = base.username;
// 一次性密码：来自设置面板输入或旧版 settings.json 迁移，用完即弃，绝不写盘
let pendingPassword = null;
let effServers = base.servers;
let manualReconnect = false;

const settingsFile = () => path.join(app.getPath("userData"), "settings.json");

function saveSettingsFile() {
  fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
  fs.writeFileSync(settingsFile(), JSON.stringify(settings, null, 2));
}

function loadSettings() {
  try {
    Object.assign(settings, JSON.parse(fs.readFileSync(settingsFile(), "utf8")));
  } catch {
    /* first run: keep defaults */
  }
  // 旧版 settings.json 里可能存着密码：迁移成一次性凭据并从磁盘抹掉
  if (settings.password) {
    pendingPassword = settings.password;
    delete settings.password;
    saveSettingsFile();
  }
}

function applyConfig() {
  effUsername = settings.username || base.username;
  const doms = (settings.domains || "")
    .split(",")
    .map((d) => d.trim())
    .filter(Boolean);
  effServers = doms.length ? doms.map(base.toServer) : base.servers;
}

// ---------------------------------------------------------------- state

let mainWindow = null;
let popup = null;
let callPopup = null;
let tray = null;
let ws = null;
let serverIndex = 0;
let failedAttempts = 0;
let reconnectTimer = null;
const queue = [];
let popupBusy = false;
let latestVersion = null; // 检查更新发现的最新版本号，null 表示已是最新

const currentBase = () => effServers[serverIndex].http;
const currentWs = () =>
  `${effServers[serverIndex].ws}/ws?token=${encodeURIComponent(effToken)}`;
const currentName = () => effServers[serverIndex].http.replace(/^\w+:\/\//, "");

// ---------------------------------------------------------------- windows

// 渲染进程最小权限：只用 contextBridge 暴露的 IPC，禁 Node、禁新开窗口/导航
const SECURE_WEBPREFS = {
  preload: path.join(__dirname, "preload.js"),
  contextIsolation: true,
  nodeIntegration: false,
  sandbox: true,
};

function hardenWindow(win) {
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (e) => e.preventDefault());
}

function createPopup() {
  popup = new BrowserWindow({
    width: 380,
    height: 210,
    frame: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    focusable: true,
    webPreferences: { ...SECURE_WEBPREFS },
  });
  hardenWindow(popup);
  popup.loadFile(path.join(__dirname, "popup.html"));
  popup.on("closed", () => {
    popup = null;
    popupBusy = false;
    setTimeout(showNext, 200);
  });
}

function positionPopup() {
  const { workAreaSize, workArea } = screen.getPrimaryDisplay();
  const [w, h] = popup.getSize();
  popup.setPosition(
    workArea.x + workAreaSize.width - w - 14,
    workArea.y + workAreaSize.height - h - 14
  );
}

function showNext() {
  if (!queue.length || popupBusy) return;
  if (!popup) createPopup();
  popupBusy = true;
  const sms = queue.shift();
  const send = () => {
    popup.webContents.send("sms", {
      sms,
      remaining: queue.length,
      sound: settings.soundEnabled,
    });
    positionPopup();
    popup.showInactive();
  };
  popup.webContents.isLoading()
    ? popup.webContents.once("did-finish-load", send)
    : send();
}

function showCallPopup(call) {
  if (!callPopup) {
    callPopup = new BrowserWindow({
      width: 340,
      height: 220,
      frame: false,
      resizable: false,
      alwaysOnTop: true,
      skipTaskbar: true,
      show: false,
      webPreferences: { ...SECURE_WEBPREFS },
    });
    hardenWindow(callPopup);
    callPopup.loadFile(path.join(__dirname, "callpopup.html"));
    callPopup.on("closed", () => (callPopup = null));
  }
  const { workAreaSize, workArea } = screen.getPrimaryDisplay();
  const [w, h] = callPopup.getSize();
  callPopup.setPosition(
    workArea.x + workAreaSize.width - w - 14,
    workArea.y + workAreaSize.height - h - 14
  );
  const send = () => {
    callPopup.webContents.send("call", { call });
    callPopup.showInactive();
  };
  callPopup.webContents.isLoading()
    ? callPopup.webContents.once("did-finish-load", send)
    : send();
}

function handleCallEvent(call) {
  if (mainWindow) mainWindow.webContents.send("call-event", call);
  if (call.direction === "in" && call.status === "ringing") {
    showCallPopup(call);
  } else if (callPopup) {
    callPopup.webContents.send("call-update", call);
  }
}

function openMainWindow(showSettings = false) {  if (mainWindow) {
    mainWindow.show();
    mainWindow.focus();
    if (showSettings) mainWindow.webContents.send("show-settings");
    return;
  }
  mainWindow = new BrowserWindow({
    width: 860,
    height: 580,
    minWidth: 640,
    minHeight: 420,
    title: "SMSync",
    backgroundColor: "#0d1117",
    webPreferences: { ...SECURE_WEBPREFS },
  });
  hardenWindow(mainWindow);
  mainWindow.loadFile(path.join(__dirname, "index.html"));
  if (showSettings) {
    mainWindow.webContents.once("did-finish-load", () =>
      mainWindow.webContents.send("show-settings")
    );
  }
  mainWindow.on("closed", () => (mainWindow = null));
}

// ---------------------------------------------------------------- websocket

function broadcastStatus(connected) {
  const status = { connected, domain: currentName() };
  for (const win of [mainWindow, popup]) {
    if (win) win.webContents.send("status", status);
  }
}

function scheduleReconnect(delayMs) {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, delayMs);
}

// 认证：优先用 refreshToken 无状态续期（软轮换，总是保存最新的）；
// 没有（可用）refreshToken 才用一次性密码（设置面板输入 / 旧版迁移 / .env）走机器通道登录。
// 失败同样计入故障转移次数
let loginPromise = null;
function login() {
  if (loginPromise) return loginPromise; // 避免并发重复登录
  loginPromise = (async () => {
    try {
      if (settings.refreshToken) {
        try {
          const r = await fetch(`${currentBase()}/api/v1/auth/refresh`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: settings.refreshToken }),
          });
          if (r.ok) {
            applyTokens(await r.json());
            console.log(`token refreshed: ${currentName()}`);
            return true;
          }
          if (r.status === 401) {
            // refresh 也失效：清掉，落到下面的密码通道或等用户重新输密码
            settings.refreshToken = "";
            saveSettingsFile();
            console.error("refresh token 失效 - 请在设置里重新输入密码");
          } else {
            throw new Error(`HTTP ${r.status}`);
          }
        } catch (e) {
          return authFailed(e); // 网络错误等：保留 refreshToken，下次再试
        }
      }
      // 没有（可用）refreshToken：用一次性密码登录换一对令牌
      const password = pendingPassword || base.password;
      if (!effUsername || !password) {
        console.error("需要密码登录 - 请在设置里输入密码，或在 .env 配置 SMSYNC_PASSWORD");
        if (!ws || ws.readyState !== WebSocket.OPEN) scheduleReconnect(15000);
        return false;
      }
      try {
        const r = await fetch(`${currentBase()}/api/v1/auth/token`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: effUsername, password }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        applyTokens(data);
        pendingPassword = null; // 密码只用一次，用完即弃
        console.log(`login ok: ${currentName()} (${data.user?.username || effUsername})`);
        return true;
      } catch (e) {
        return authFailed(e);
      }
    } finally {
      loginPromise = null;
    }
  })();
  return loginPromise;
}

// 保存新的一对令牌：access token 只在内存，refreshToken 持久化到 settings.json
function applyTokens(data) {
  effToken = data.access_token;
  if (data.refresh_token) settings.refreshToken = data.refresh_token;
  saveSettingsFile();
}

// 认证失败统一处理：清 access token、计入故障转移、安排重试
function authFailed(e) {
  effToken = null;
  console.error(`auth failed: ${currentName()} ${e.message}`);
  failedAttempts += 1;
  if (failedAttempts >= 3) {
    serverIndex = (serverIndex + 1) % effServers.length;
    failedAttempts = 0;
    console.log(`failover -> ${currentName()}`);
  }
  // WS 还连着时（relogin 场景）不重复建连，只等下次自然重连
  if (!ws || ws.readyState !== WebSocket.OPEN) scheduleReconnect(3000);
  return false;
}

async function connect() {
  if (!effToken) {
    if (!settings.refreshToken && !pendingPassword && !base.password) {
      console.error("no credentials - open settings and enter password, or edit .env");
      scheduleReconnect(15000);
      return;
    }
    // 没有 access token 先走认证（refresh 续期或密码登录）；失败时 login() 内部已安排重试/故障转移
    if (!(await login())) return;
  }
  try {
    ws = new WebSocket(currentWs());
  } catch {
    return scheduleReconnect(3000);
  }
  ws.on("open", () => {
    failedAttempts = 0;
    console.log(`connected: ${currentName()}`);
    broadcastStatus(true);
    checkUpdate();
  });
  ws.on("message", (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }
    if (msg.type === "sms") {
      if (settings.popupEnabled) {
        queue.push(msg.data);
        showNext();
      }
      if (mainWindow) mainWindow.webContents.send("sms", { sms: msg.data });
    } else if (msg.type === "delete") {
      if (mainWindow) mainWindow.webContents.send("sms-deleted", { id: msg.data.id });
    } else if (msg.type === "call") {
      handleCallEvent(msg.data);
    } else if (msg.type === "sms_sent") {
      if (mainWindow) mainWindow.webContents.send("sms-sent", msg.data);
    } else if (msg.type === "agent") {
      if (mainWindow) mainWindow.webContents.send("agent-status", msg.data);
    }
  });
  const onLost = (code) => {
    broadcastStatus(false);
    if (manualReconnect) {
      manualReconnect = false;
      return scheduleReconnect(200);
    }
    if (code === 4401) {
      // JWT 过期/被吊销：清掉令牌，重连时 connect() 会先重新登录
      effToken = null;
      return scheduleReconnect(1000);
    }
    failedAttempts += 1;
    if (failedAttempts >= 3) {
      serverIndex = (serverIndex + 1) % effServers.length;
      failedAttempts = 0;
      console.log(`failover -> ${currentName()}`);
    }
    scheduleReconnect(3000);
  };
  ws.on("close", onLost);
  ws.on("error", () => ws.terminate());
}

function reconnect() {
  manualReconnect = true;
  serverIndex = 0;
  failedAttempts = 0;
  effToken = null; // 凭据可能已改，重连前强制重新登录
  if (ws) ws.terminate();
  else scheduleReconnect(0);
}

// ---------------------------------------------------------------- update check

// 简单 semver 比较：a 比 b 新则返回 true（按 . 分段逐段比数字）
function isNewer(a, b) {
  const pa = String(a).split(".").map(Number);
  const pb = String(b).split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0;
    const y = pb[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}

// health 接口无需鉴权；服务端版本更新时记下来并通知主窗口
async function checkUpdate() {
  try {
    const r = await fetch(`${currentBase()}/api/v1/health`);
    if (!r.ok) return;
    const data = await r.json();
    if (data.version && isNewer(data.version, app.getVersion())) {
      if (latestVersion !== data.version) {
        latestVersion = data.version;
        console.log(`update available: v${latestVersion}`);
        if (mainWindow) mainWindow.webContents.send("update-available", latestVersion);
      }
    } else {
      latestVersion = null;
    }
  } catch {
    /* 离线时跳过，等下次检查 */
  }
}

// ---------------------------------------------------------------- ipc

ipcMain.handle("copy-text", (event, text) => {
  clipboard.writeText(String(text));
  event.sender.send("copied");
});

ipcMain.handle("get-config", async () => {
  // renderer 拿配置去发 REST 请求，确保已有 access token
  if (!effToken) await login();
  return {
    baseUrl: currentBase(),
    token: effToken,
    domain: currentName(),
    version: app.getVersion(),
    latestVersion, // 有更新时为最新版本号，否则 null
  };
});

// renderer 的 REST 请求遇到 401 时调用：走 refresh 续期，失败返回 null
ipcMain.handle("relogin", async () => {
  effToken = null;
  return (await login()) ? effToken : null;
});

ipcMain.on("open-releases", () => {
  shell.openExternal("https://github.com/idcim/smsync/releases");
});

ipcMain.handle("get-settings", () => {
  // refreshToken 是敏感凭据，不下发给渲染进程
  const { refreshToken, ...rest } = settings;
  return {
    ...rest,
    // show what will actually be used, so the user can tell env fallback apart
    effectiveDomains: effServers.map((s) => s.http.replace(/^\w+:\/\//, "")).join(", "),
  };
});

ipcMain.handle("save-settings", (_e, patch) => {
  // 密码只做一次性登录凭据，不进 DEFAULT_SETTINGS、不写盘
  const oneShot = typeof patch.password === "string" && patch.password ? patch.password : null;
  const userChanged = "username" in patch && patch.username !== settings.username;
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    if (key in patch) settings[key] = patch[key];
  }
  // 换了账号或输入了新密码：旧 refreshToken 作废，改用密码重新登录
  if (oneShot || userChanged) settings.refreshToken = "";
  if (oneShot) pendingPassword = oneShot;
  saveSettingsFile();
  applyConfig();
  app.setLoginItemSettings({ openAtLogin: Boolean(settings.autoStart) });
  reconnect();
  return true;
});

ipcMain.on("test-popup", () => {
  queue.push({
    id: Date.now(),
    sender: "SMSync 测试",
    text: "这是一条测试短信，您的验证码是 123456，5分钟内有效。",
    received_at: new Date().toISOString(),
  });
  showNext();
});

ipcMain.on("open-main", () => {
  if (popup) popup.hide();
  popupBusy = false;
  openMainWindow();
  setTimeout(showNext, 300);
});

ipcMain.on("popup-dismiss", () => {
  if (popup) popup.hide();
  popupBusy = false;
  setTimeout(showNext, 300);
});

// ---------------------------------------------------------------- lifecycle

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.whenReady().then(() => {
    loadSettings();
    applyConfig();
    app.setLoginItemSettings({ openAtLogin: Boolean(settings.autoStart) });
    const bundledIcon = path.join(__dirname, "..", "build", "icon.png");
    const icon = fs.existsSync(bundledIcon)
      ? nativeImage.createFromPath(bundledIcon)
      : nativeImage.createFromDataURL(
          "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        );
    tray = new Tray(icon);
    tray.setToolTip("SMSync");
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: "打开 SMSync", click: () => openMainWindow() },
        { label: "设置", click: () => openMainWindow(true) },
        { label: "测试弹窗", click: () => ipcMain.emit("test-popup") },
        { type: "separator" },
        { label: "退出", click: () => app.exit(0) },
      ])
    );
    tray.on("click", () => openMainWindow());
    connect();
    setInterval(checkUpdate, 6 * 60 * 60 * 1000); // 之后每 6 小时检查一次更新
    if (process.argv.includes("--show")) openMainWindow();
  });
  app.on("window-all-closed", () => {
    /* keep running in tray */
  });
}
