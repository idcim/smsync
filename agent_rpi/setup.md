# 树莓派 + EC20 部署指南（SMSync agent_rpi）

> **必读前置条件**
> 1. SIM 卡必须开通**语音通话**功能（纯流量卡/大多数物联网卡只有短信和流量，打不了电话）
> 2. EC20 语音走 **VoLTE**，当地运营商需支持且 SIM 已开通 VoLTE
> 3. 通话音频走 EC20 的 **PCM 引脚 ↔ 树莓派 I2S**，需要 EC20 开发板或 PCM 引脚已引出的模块；
>    只做短信收发和呼叫控制（拨号/接听/挂断/来电通知/录音不可用）时，纯 USB 连接即可

## 1. 硬件接线

### USB（基础功能：短信收发 + 呼叫控制 + 来电通知）

EC20 模块 USB 直连树莓派 USB 口。枚举出 `/dev/ttyUSB0~3`，AT 口通常是 **ttyUSB2**：

```bash
ls /dev/ttyUSB*
# 逐个试：picocom /dev/ttyUSB2 -b 115200，输入 AT 回显 OK 即为 AT 口
```

### PCM ↔ I2S（通话音频，可选）

| EC20 引脚 | 树莓派引脚 (GPIO) | 说明 |
|-----------|------------------|------|
| PCM_CLK  | GPIO18 (PCM_CLK, pin 12) | 位时钟 |
| PCM_SYNC | GPIO19 (PCM_FS, pin 35)  | 帧同步 |
| PCM_DOUT | GPIO20 (PCM_DIN, pin 38) | EC20 输出 → RPi 输入 |
| PCM_DIN  | GPIO21 (PCM_DOUT, pin 40)| RPi 输出 → EC20 输入 |
| GND      | GND                        | 共地必须接 |

模块侧配置（在 AT 口执行一次，保存在 NVRAM）：

```
AT+QDAI=3,0,0,2,0,1,1,0   # PCM 主模式等参数，具体以手中固件的 Quectel 语音应用笔记为准
AT+QCFG="volte",1          # 开启 VoLTE（部分固件需重启生效）
```

树莓派侧 `/boot/firmware/config.txt`（老系统为 `/boot/config.txt`）启用 I2S：

```
dtparam=i2s=on
dtoverlay=rpi-simple-soundcard   # 或按所用 overlay 文档配置，使 PCM 注册为 ALSA 声卡
```

重启后 `arecord -l` 应能看到新声卡，把卡名填进 `config.ini` 的 `[audio] device`。

### 本地双向通话（可选）

要在树莓派上直接接打（麦克风+扬声器），用 `alsaloop` 在 PCM 声卡和 USB 声卡之间做双向回环，
建议做成 systemd 服务常驻（agent 只负责录音，不负责回环）：

```bash
alsaloop -C hw: pcmcard -P hw: usbcard -r 8000 -c 1 -f S16_LE
```

## 2. 安装

```bash
sudo apt install -y python3-pip alsa-utils
pip install -r requirements.txt   # pyserial, websocket-client
cp config.example.ini config.ini  # 填服务器地址和 token
python agent.py
```

## 3. systemd 常驻

`/etc/systemd/system/smsync-agent.service`：

```ini
[Unit]
Description=SMSync RPi Agent
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/home/pi/smsync/agent_rpi
ExecStart=/usr/bin/python3 agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now smsync-agent
journalctl -u smsync-agent -f
```

## 4. 现场验证清单

1. `AT` → OK；`AT+CPIN?` → READY；`AT+CSQ` 信号正常
2. 手机拨打模块号码 → 客户端弹出来电通知（号码正确）
3. 客户端点"接听" → 通话建立，录音开始；挂断 → 通话记录出现在客户端，录音可播放
4. 客户端拨号 → 手机响铃
5. 客户端发短信 → 手机收到（中文正常）
6. 拔掉网线重试 → 短信进本地队列，恢复后补传
