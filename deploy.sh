#!/usr/bin/env bash
# 1Panel 计划任务（Shell 脚本类型）定时调用：
#   cd /opt/smsync && ./deploy.sh
# 逻辑：先 fetch 比对远端，有新提交才 pull + 重新部署，否则直接跳过。
set -e
cd "$(dirname "$0")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

git fetch --quiet origin

LOCAL=$(git rev-parse HEAD)
BRANCH=$(git branch --show-current)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || true)

if [ -z "$REMOTE" ]; then
  log "no upstream for branch '$BRANCH', skip"
  exit 0
fi

if [ "$LOCAL" = "$REMOTE" ]; then
  log "already up to date (${LOCAL:0:8}), skip"
  exit 0
fi

if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
  log "ERROR: local branch has diverged from origin/$BRANCH, manual fix required"
  exit 1
fi

log "update available: ${LOCAL:0:8} -> ${REMOTE:0:8}, deploying..."
git pull --ff-only
docker compose up -d --build
log "deploy finished"
