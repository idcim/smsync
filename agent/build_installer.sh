#!/usr/bin/env bash
# 打包 SMSync Agent 为 Windows 安装包：
#   1. PyInstaller 打包成单个 exe（agent/dist/smsync-agent.exe）
#   2. makensis 编译 installer.nsi 生成 agent/dist/SMSyncAgent-Setup-<version>.exe
# 用法（Git Bash）:  agent/build_installer.sh
set -euo pipefail
cd "$(dirname "$0")"

MAKENSIS="${MAKENSIS:-$LOCALAPPDATA/electron-builder/cache/nsis-3.0.4.1/nsis-3.0.4.1-1mx3n/makensis.exe}"

pyinstaller --noconfirm --clean --onefile --name smsync-agent \
  --add-data "$(pwd -W)/config.example.ini;." \
  --distpath dist --workpath build --specpath build \
  agent.py

if [ ! -x "$MAKENSIS" ] && [ ! -f "$MAKENSIS" ]; then
  echo "makensis 未找到: $MAKENSIS"
  echo "请安装 NSIS 或设置 MAKENSIS 环境变量指向 makensis.exe"
  exit 1
fi

# NSIS 3.x 需要 UTF-8 BOM 才能正确处理中文，编辑器改动后可能丢失，构建前自动补上
python -c "
data = open('installer.nsi','rb').read()
if not data.startswith(b'\xef\xbb\xbf'):
    open('installer.nsi','wb').write(b'\xef\xbb\xbf' + data)
" 2>/dev/null || true

"$MAKENSIS" installer.nsi
echo "OK: dist/SMSyncAgent-Setup-*.exe"
