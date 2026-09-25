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
  Tray,
  Menu,
} = require("electron");
const WebSocket = require("ws");

const base = require("./config");

// ---------------------------------------------------------------- settings

const DEFAULT_SETTINGS = {
  username: "", // empty -> fall back to env / .env
  password: "", // empty -> fall back to env / .env
  domains: "", // comma separated, empty -> env / built-in defaults
  popupEnabled: true,
  soundEnabled: true,
  autoStart: false,
};
let settings = { ...DEFAULT_SETTINGS };
let effToken = null; // 登录后拿到的 JWT，不再是静态 token
let effUsername = base.username;
let effPassword = base.password;
let effServers = base.servers;
let manualReconnect = false;

const settingsFile = () => path.join(app.getPath("userData"), "settings.json");

function loadSettings() {
  try {
    Object.assign(settings, JSON.parse(fs.readFileSync(settingsFile(), "utf8")));
  } catch {
    /* first run: keep defaults */
  }
}

function applyConfig() {
  effUsername = settings.username || base.username;
  effPassword = settings.password || base.password;
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

// 用保存的用户名密码向当前服务器换 JWT；失败同样计入故障转移次数
let loginPromise = null;
function login() {
  if (loginPromise) return loginPromise; // 避免并发重复登录
  loginPromise = (async () => {
    try {
      const r = await fetch(`${currentBase()}/api/v1/auth/token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: effUsername, password: effPassword }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      effToken = data.access_token;
      console.log(`login ok: ${currentName()} (${data.user?.username || effUsername})`);
      return true;
    } catch (e) {
      effToken = null;
      console.error(`login failed: ${currentName()} ${e.message}`);
      failedAttempts += 1;
      if (failedAttempts >= 3) {
        serverIndex = (serverIndex + 1) % effServers.length;
        failedAttempts = 0;
        console.log(`failover -> ${currentName()}`);
      }
      // WS 还连着时（relogin 场景）不重复建连，只等下次自然重连
      if (!ws || ws.readyState !== WebSocket.OPEN) scheduleReconnect(3000);
      return false;
    } finally {
      loginPromise = null;
    }
  })();
  return loginPromise;
}

async function connect() {
  if (!effToken) {
    if (!effUsername || !effPassword) {
      console.error("SMSYNC_USERNAME/SMSYNC_PASSWORD not set - open settings or edit .env");
      scheduleReconnect(15000);
      return;
    }
    // 没有 JWT 先登录；失败时 login() 内部已安排重试/故障转移
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

// ---------------------------------------------------------------- ipc

ipcMain.handle("copy-text", (event, text) => {
  clipboard.writeText(String(text));
  event.sender.send("copied");
});

ipcMain.handle("get-config", async () => {
  // renderer 拿配置去发 REST 请求，确保已有 JWT
  if (!effToken && effUsername && effPassword) await login();
  return {
    baseUrl: currentBase(),
    token: effToken,
    domain: currentName(),
    version: app.getVersion(),
  };
});

// renderer 的 REST 请求遇到 401 时调用：重新登录并返回新 JWT
ipcMain.handle("relogin", async () => {
  effToken = null;
  return (await login()) ? effToken : null;
});

ipcMain.handle("get-settings", () => ({
  ...settings,
  // show what will actually be used, so the user can tell env fallback apart
  effectiveDomains: effServers.map((s) => s.http.replace(/^\w+:\/\//, "")).join(", "),
}));

ipcMain.handle("save-settings", (_e, patch) => {
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    if (key in patch) settings[key] = patch[key];
  }
  fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
  fs.writeFileSync(settingsFile(), JSON.stringify(settings, null, 2));
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
    if (process.argv.includes("--show")) openMainWindow();
  });
  app.on("window-all-closed", () => {
    /* keep running in tray */
  });
}
