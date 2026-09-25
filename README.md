# SMSync — EC20 短信实时同步系统

通过插在电脑上的 Quectel EC20 4G 模块接收短信，实时同步到服务器，再推送到各客户端：PC 弹窗（Electron / Python）、手机网页（PWA），支持验证码一键复制。

## 架构

```
EC20 (COM9)                       服务器 (Docker)                        客户端
┌──────────┐   HTTPS POST       ┌──────────────────────┐   WebSocket    ┌────────────────┐
│ agent/   │ ─────────────────▶ │ server/ (FastAPI)    │ ─────────────▶ │ Electron 客户端 │
│ 采集端    │  失败进本地队列     │  REST API + SQLite   │                │ (弹窗+复制验证码)│
│          │ ◀───────────────── │  WebSocket 推送       │ ─────────────▶ │ 手机 PWA 网页   │
└──────────┘  定时重传           └──────────────────────┘                │ Python 通知器   │
                                          ▲                             └────────────────┘
                                          └──── GET /api/v1/sms 任意客户端随时拉取
```

## 目录结构

```
├── agent/              # 采集端：跑在插 EC20 的电脑上
│   ├── agent.py        #   主程序：监听新短信 → 上传，断线自动重连
│   ├── modem.py        #   EC20 AT 指令层（文本模式 + UCS2 中文解码）
│   ├── outbox.py       #   本地 SQLite 队列：断网暂存、恢复后重传
│   └── config.ini      #   串口、服务器地址、token
├── server/             # 服务器：FastAPI + SQLite + WebSocket + 网页客户端
│   ├── app.py          #   REST API + WS 推送 + PWA 托管
│   ├── static/         #   网页客户端（手机/PC 通用）
│   ├── Dockerfile
│   └── .token          #   本地运行时自动生成的访问令牌
├── agent_rpi/          # 树莓派采集端：SMS 收发 + 语音通话控制 + 通话录音
│   ├── agent.py        #   主程序（短信/来电监听/指令执行/断网队列）
│   ├── voice.py        #   通话状态机（RING/CLIP/CLCC 驱动）
│   ├── audio.py        #   通话录音（arecord/ALSA）
│   ├── uplink.py       #   WS 下行指令通道（拨号/接听/挂断/发短信）
│   └── setup.md        #   树莓派接线（PCM↔I2S）、VoLTE、systemd 部署指南
├── client_electron/    # Electron 桌面客户端（推荐）
│   └── src/            #   弹窗 / 主窗口(短信/通话/发短信) / 双域名故障转移 / 设置
├── client_pc/          # Python 轻量通知器（无界面，系统通知弹窗）
├── docker-compose.yml  # 服务器一键部署
├── deploy.sh           # git pull + 重新部署（供 1Panel 计划任务调用）
└── .env.example        # 服务器环境变量模板
```

## 快速开始（本地开发）

### 1. 服务器

```bash
cd server
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

首次启动自动生成访问令牌，保存在 `server/.token`（也可用环境变量 `SMSYNC_TOKEN` 指定）。

### 2. 采集端（插 EC20 的电脑）

```bash
cd agent
pip install -r requirements.txt
# 编辑 config.ini：modem 端口（设备管理器里 "Quectel USB AT Port"，本机为 COM9）、
# 服务器 url、token（与 server/.token 一致）
python agent.py
```

插入 SIM 卡后自动进入监听；新短信实时上传，断网时本地暂存、恢复后自动补传。

#### 打包成 Windows 安装包（免 Python 环境分发）

```bash
agent/build_installer.sh   # Git Bash 运行，输出 agent/dist/SMSyncAgent-Setup-<版本号>.exe
```

先用 PyInstaller 打成单文件 `smsync-agent.exe`，再用 NSIS 生成安装包（makensis 默认取 electron-builder 缓存里的，也可用 `MAKENSIS` 环境变量指定）。安装包特性：

- 自定义安装目录、开始菜单快捷方式、控制面板"卸载程序"入口
- 可选组件"开机自动启动"（写注册表 Run 项，卸载时清除）
- 首次安装自动放置 `config.ini` 模板，**重装保留已有配置**；卸载保留 `config.ini` 和 `%APPDATA%\SMSyncAgent`（本地队列 `outbox.db` 与日志 `agent.log`）
- 版本号在 `agent/agent.py` 的 `__version__` 和 `agent/installer.nsi` 的 `APP_VERSION`，两处需同步

### 3. Electron 客户端

```bash
cd client_electron
npm install
cp .env.example .env   # 填入 SMSYNC_TOKEN
npm start              # 常驻系统托盘；npm start -- --show 同时打开主窗口
```

- 新短信 → 右下角弹窗，**复制验证码 / 复制全文** 一键完成，点击内容打开主窗口
- 工具栏：搜索、刷新、复制最新验证码、测试弹窗、设置
- 设置里可改 token、域名（默认 `smsync.h6.fan`，备用 `smsync.qisop.com`，自动故障转移）、弹窗/提示音开关、**开机自启**（应用内核原生实现，无需手动加注册表/计划任务）

### 打包 Windows 安装包

```bash
cd client_electron
npm run icon   # 可选：重新生成 build/icon.png
npm run dist   # 输出 dist/SMSync Setup <版本号>.exe（NSIS 安装包）
```

版本号在 `client_electron/package.json` 的 `version` 字段，主窗口标题栏和设置面板底部都会显示。安装包支持自定义安装目录、自动创建桌面/开始菜单快捷方式；安装后运行即为托盘常驻，开机自启在应用内"设置"里勾选。

### 4. 其他客户端（可选）

- **手机/网页**：浏览器打开 `http://服务器地址:8000`，输入 token，点"开启通知"，可"添加到主屏幕"装成 APP
- **Python 通知器**：`cd client_pc && pip install -r requirements.txt && python notifier.py`

## 服务器部署（1Panel + Docker）

```bash
git clone <仓库地址> /opt/smsync && cd /opt/smsync
cp .env.example .env    # 填入正式 SMSYNC_TOKEN
docker compose up -d
```

- **定时拉取部署**：1Panel → 计划任务 → Shell 脚本，内容 `cd /opt/smsync && ./deploy.sh`，周期自定。脚本会先 `git fetch` 比对远端：**有新提交才** `git pull` + `docker compose up -d --build`，无更新直接跳过（日志会打印 skip 原因）；本地分支与远端分叉时会报错退出，防止误部署
- **域名与 HTTPS**：1Panel → 网站 → 反向代理，把 `smsync.h6.fan`、`smsync.qisop.com` 代理到 `127.0.0.1:8000`，申请 SSL 并确认开启 WebSocket 支持
- 上线后把 `agent/config.ini` 和各客户端的地址改为 `https://smsync.h6.fan`

## API

所有接口需要请求头 `Authorization: Bearer <SMSYNC_TOKEN>`。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/health` | 健康检查（无需鉴权） |
| POST | `/api/v1/sms` | 上报短信 `{sender, text, received_at?, client_msg_id?}`；`client_msg_id` 幂等去重 |
| GET | `/api/v1/sms?limit=&before_id=` | 分页拉取（倒序） |
| GET | `/api/v1/sms/{id}` | 单条详情 |
| DELETE | `/api/v1/sms/{id}` | 删除短信，并广播 `{"type":"delete","data":{"id":N}}` 让各客户端实时移除 |
| POST | `/api/v1/sms/send` | 发短信 `{to, text}`（经 agent 下行执行，结果写入发件箱） |
| GET | `/api/v1/sms/outbox/list` | 发件箱（sent/failed/pending） |
| POST | `/api/v1/calls` | agent 上报通话事件（按 `client_msg_id` 幂等 upsert） |
| GET | `/api/v1/calls` | 通话记录 |
| POST | `/api/v1/calls/dial` `/answer` `/hangup` | 呼叫控制（转发 agent，agent 离线返回 503） |
| POST/GET | `/api/v1/calls/{id}/recording` | 上传 / 播放通话录音（WAV） |
| WS | `/ws?token=` | 客户端推送：`sms` / `delete` / `call` / `sms_sent` / `agent` |
| WS | `/ws/agent?token=` | agent 指令通道（下行命令 + ack） |

## 语音通话（树莓派）

树莓派接 EC20（详见 `agent_rpi/setup.md`）：USB 提供短信收发、来电通知、拨号/接听/挂断控制；
通话音频需 EC20 PCM 引脚接树莓派 I2S（接线表和配置在 setup.md），支持通话录音自动上传、
客户端在线播放。远程实时通话（在 PC/手机上直接对话）需 WebRTC，为后续版本候选。

> 前置条件：SIM 卡须开通语音 + VoLTE；纯流量/物联网卡只能收发短信。

## 开机自启（Windows，非 Docker 方式）

服务器和采集端可用计划任务（Electron 客户端不需要——在应用内"设置"里勾选"开机自动启动"即可）：

```bash
schtasks /create /tn SMSyncServer /tr "cmd /c cd /d D:\DEV_AI\smsync\server && C:\path\to\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000" /sc onlogon
```

采集端同理换成 `cmd /c cd /d D:\DEV_AI\smsync\agent && python.exe agent.py`。

## 已知限制

- 超长拼接短信会按段分别上报（未做 UDH 重组）
- 网页端手机通知依赖浏览器 Notification API，页面需保持打开（或装为 PWA）；锁屏强提醒可接 Bark/Server酱 等 webhook 转发
- 验证码提取规则：优先匹配"验证码/校验码/动态码/code"附近的 4-8 位数字，兜底独立 6 位 / 4 位数字
