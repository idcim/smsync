# AGENTS.md — 面向 AI 编码代理的项目指南

> 本文件供 AI 编码代理和新协作者阅读。用户文档见 `README.md`；本文件侧重架构心智模型、组件边界、命令与禁区。

## 项目概述

SMSync：Quectel EC20 4G 模块的短信/通话实时同步系统。采集端通过串口 AT 指令读取短信，上传到 FastAPI 服务器（SQLite 存储），再通过 WebSocket 推送到各客户端（Electron 桌面端、手机 PWA、Python 通知器）。

## 架构与数据流

```
EC20 模块 ──串口──▶ agent/ 或 agent_rpi/ ──HTTPS POST──▶ server/ ──WS 广播──▶ 客户端
                         ▲                                    │
                         └──── WS /ws/agent 下行指令 ◀────────┘
                            （发短信 / 拨号 / 接听 / 挂断）
```

- 上行：agent 监听新短信/来电 → POST `/api/v1/sms`、`/api/v1/calls` → server 存 SQLite 并向 `/ws` 广播
- 下行：客户端调 `/api/v1/sms/send`、`/api/v1/calls/dial` 等 → server 经 `/ws/agent` 转发给 agent 执行
- 断网容错：agent 本地 `outbox.db` 队列暂存，恢复后自动补传；上报用 `client_msg_id` 幂等去重

## 组件清单

| 目录 | 职责 | 入口 | 技术栈 |
|------|------|------|--------|
| `server/` | REST API + SQLite + WS 推送 + PWA 托管 | `app:app`（uvicorn，端口 8000） | FastAPI, Pydantic |
| `agent/` | Windows 采集端（插 EC20 的电脑） | `agent.py` | pyserial, websocket-client |
| `agent_rpi/` | 树莓派采集端：短信 + 语音通话 + 录音 | `agent.py` | pyserial, websocket-client, ALSA |
| `client_electron/` | 桌面客户端：弹窗 + 验证码复制 + 主窗口 | `src/main.js` | Electron 37, ws |
| `client_pc/` | 轻量 Python 通知器（系统弹窗，无界面） | `notifier.py` | windows-toasts, websocket-client |

关键模块：

- `agent/modem.py` / `agent_rpi/modem.py`：EC20 AT 指令层，文本模式 + UCS2 中文解码
- `agent/outbox.py`：本地 SQLite 断网队列
- `agent_rpi/voice.py`：通话状态机（RING/CLIP/CLCC 驱动）
- `agent_rpi/audio.py`：通话录音（arecord/ALSA，WAV）
- `agent_rpi/uplink.py`：WS 下行指令通道（拨号/接听/挂断/发短信）
- `server/app.py`：全部路由 + WS 管理；`server/db.py`：存储层；`server/config.py`：token/路径

## 常用命令

```bash
# 服务器（本地开发）
cd server && pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000

# 采集端（先按 config.example.ini 配好 config.ini）
cd agent && pip install -r requirements.txt && python agent.py

# 采集端打包 Windows 安装包（PyInstaller + NSIS，产物在 agent/dist/）
agent/build_installer.sh   # 版本号：agent.py 的 __version__ 与 installer.nsi 的 APP_VERSION 同步

# Electron 客户端
cd client_electron && npm install
npm start              # 常驻托盘；npm start -- --show 同时开主窗口
npm run icon           # 重新生成 build/icon.png（可选）
npm run dist           # 打包 NSIS 安装包到 dist/

# Docker 部署（服务器）
docker compose up -d

# 定时增量部署（1Panel 计划任务调用）
cd /opt/smsync && ./deploy.sh
```

## 配置与密钥规则

- 访问令牌三处必须一致：`server/.token`（或环境变量 `SMSYNC_TOKEN`）= `agent/config.ini` 的 `token` = 各客户端设置里的 token
- 所有 HTTP 接口需请求头 `Authorization: Bearer <SMSYNC_TOKEN>`；WS 用 `?token=` 查询参数
- 以下文件已 gitignore，**绝不提交**：`.env`、`client_electron/.env`、`server/.token`、`agent/config.ini`、`client_pc/config.ini`、`*.db`
- 修改配置结构时，只改 `*.example` 模板文件（`config.example.ini`、`.env.example`），并在 README 说明

## 代码约定

- Python 为主；**无 lint 配置、无测试框架** —— 改动后手动跑通对应组件验证（起服务器 / 跑 agent / `npm start`）
- 注释、文档、commit message 用中文
- 上报类接口保持幂等：客户端生成 `client_msg_id`，服务端据此去重/upsert
- 任何导致数据变化的接口（新短信、删除、通话事件、发送结果）必须向 `/ws` 广播对应消息，让客户端实时同步
- WS 消息类型：`sms` / `delete` / `call` / `sms_sent` / `agent`
- Electron 版本号在 `client_electron/package.json` 的 `version` 字段，主窗口标题栏和设置面板会显示

## API 速览

完整 API 表见 `README.md`。核心端点：

- `POST /api/v1/sms`、`GET /api/v1/sms`、`DELETE /api/v1/sms/{id}`
- `POST /api/v1/sms/send`、`GET /api/v1/sms/outbox/list`
- `POST /api/v1/calls`、`GET /api/v1/calls`、`/api/v1/calls/dial|answer|hangup`
- `POST/GET /api/v1/calls/{id}/recording`（WAV 录音）
- `WS /ws?token=`（客户端推送）、`WS /ws/agent?token=`（agent 指令通道）

## 禁区与注意事项

- 不要把 token、`.env`、`config.ini`、数据库文件写进代码或提交
- 不要削弱安全控制：token 常数时间比较、号码白名单校验（防 AT 注入）、容器非 root + 只读 rootfs、Electron sandbox——任何改动都要保留等效防护，详见 `README.md` 的"安全设计"一节
- `deploy.sh` 在本地分支与远端分叉时报错退出（防误部署），**不要为它加强推/reset 逻辑**
- 改 `agent_rpi/` 的音频/通话相关代码前，先读 `agent_rpi/setup.md` 了解硬件接线（EC20 PCM ↔ 树莓派 I2S）和 VoLTE 前提
- 已知限制：超长拼接短信未做 UDH 重组（按段分别上报）；网页端通知需页面保持打开
- **跨组件改动**（如 API 字段、WS 消息格式变更）需同步修改 server + agent + 所有客户端，并更新 `README.md` 的 API 表
