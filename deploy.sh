#!/usr/bin/env bash
# 1Panel 计划任务（Shell 脚本类型）调用本脚本即可实现定时拉取并部署：
#   cd /opt/smsync && ./deploy.sh
set -e
cd "$(dirname "$0")"
git pull --ff-only
docker compose up -d --build
