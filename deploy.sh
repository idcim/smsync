#!/usr/bin/env bash
# 1Panel 计划任务（Shell 脚本类型）定时调用：
#   cd /opt/smsync && ./deploy.sh
#
# 逻辑：
#   1. fetch 远端，比对本地 HEAD；
#   2. 有新提交则 ff-only 拉取（本地分叉则报错退出，不自动合并）；
#   3. HEAD 与 .last_deployed 相同 → 直接退出，连 docker compose 都不执行；
#   4. HEAD 不同 → docker compose up -d --build；
#   5. 只有部署成功才写 marker，失败则下一轮自动重试。
#
# 依赖：git、docker（compose v2 插件或独立 docker-compose）

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

readonly MARKER=".last_deployed"
readonly LOCKFILE=".deploy.lock"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ---------- 单实例锁：避免上一轮未结束又被下一次 cron 触发 ----------
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  log "another deploy is running, skip"
  exit 0
fi

# ---------- 选择 docker compose 命令 ----------
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  die "docker compose not found"
fi

# ---------- 拉取远端 ----------
if ! git fetch --quiet origin; then
  log "git fetch failed (network?), skip this round"
  exit 0
fi

BRANCH=$(git symbolic-ref --short -q HEAD) || die "detached HEAD, refuse to deploy"
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null) || die "no upstream origin/$BRANCH"
LOCAL=$(git rev-parse HEAD)

if [[ "$LOCAL" != "$REMOTE" ]]; then
  if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
    die "local branch has diverged from origin/$BRANCH, manual fix required"
  fi
  log "update available: ${LOCAL:0:8} -> ${REMOTE:0:8}"
  git pull --ff-only
fi

HEAD=$(git rev-parse HEAD)
LAST=$(cat "$MARKER" 2>/dev/null || true)

# ---------- 关键判断：HEAD 没变就直接退出，不碰 docker ----------
if [[ "$HEAD" == "$LAST" ]]; then
  log "no new commit (${HEAD:0:8}), skip"
  exit 0
fi

# ---------- 部署（失败不写 marker，下轮自动重试） ----------
log "deploying ${HEAD:0:8} (last: ${LAST:0:8}) ..."
if "${DC[@]}" up -d --build --remove-orphans; then
  printf '%s\n' "$HEAD" > "$MARKER.tmp" && mv -f "$MARKER.tmp" "$MARKER"
  log "deploy finished (${HEAD:0:8})"
else
  die "docker compose up failed, marker kept at '${LAST:-<none>}' for retry"
fi