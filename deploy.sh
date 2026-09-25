#!/usr/bin/env bash
# 1Panel 计划任务（Shell 脚本类型）定时调用：
#   cd /opt/smsync && ./deploy.sh
# 逻辑：先 fetch 比对远端，git 拉到新提交才重新部署；
#       用 .last_deployed 记录上次部署的提交，无更新时连 docker compose 都不执行。
set -e
cd "$(dirname "$0")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

MARKER=.last_deployed

git fetch --quiet origin || { log "git fetch failed (network?), skip this round"; exit 0; }

LOCAL=$(git rev-parse HEAD)
BRANCH=$(git branch --show-current)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || true)

if [ -z "$REMOTE" ]; then
  log "no upstream for branch '$BRANCH', skip"
  exit 0
fi

# 有更新先拉代码
if [ "$LOCAL" != "$REMOTE" ]; then
  if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
    log "ERROR: local branch has diverged from origin/$BRANCH, manual fix required"
    exit 1
  fi
  log "update available: ${LOCAL:0:8} -> ${REMOTE:0:8}"
  git pull --ff-only
fi

HEAD=$(git rev-parse HEAD)
LAST=$(cat "$MARKER" 2>/dev/null || true)

# 三条件都满足才跳过：提交没变化、上次部署成功过、容器还在跑
if [ "$HEAD" = "$LAST" ] && docker compose ps --status running -q 2>/dev/null | grep -q .; then
  log "already deployed ${HEAD:0:8}, skip"
  exit 0
fi

if [ "$HEAD" != "$LAST" ]; then
  log "deploying ${HEAD:0:8} ..."
else
  log "container not running, redeploying ${HEAD:0:8} ..."
fi
docker compose up -d --build
echo "$HEAD" > "$MARKER"
log "deploy finished (${HEAD:0:8})"
