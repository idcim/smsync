#!/usr/bin/env bash
# 1Panel 计划任务（Shell 脚本类型）定时调用：
#   cd /opt/smsync && ./deploy.sh
#
# 逻辑：
#   1. fetch 远端，拿到 origin/<branch> 的最新提交 REMOTE；
#   2. 读运行中容器的 git_commit 标签（构建时烤进镜像，见 server/Dockerfile）——
#      这是"已部署版本"的权威来源，不依赖仓库目录里的任何文件，
#      即使 /opt/smsync 被重新克隆也能正确判断；
#   3. 标签 == REMOTE → 直接退出，不 pull 也不碰 docker；
#   4. 否则 ff-only 拉取（分叉则报错退出），带 GIT_COMMIT 构建并部署。
#
# 依赖：git、docker（compose v2 插件或独立 docker-compose）

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

readonly MARKER=".last_deployed"
readonly LOCKFILE=".deploy.lock"
readonly CONTAINER="smsync-server"

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

# ---------- 拉取远端信息 ----------
if ! git fetch --quiet origin; then
  log "git fetch failed (network?), skip this round"
  exit 0
fi

BRANCH=$(git symbolic-ref --short -q HEAD) || die "detached HEAD, refuse to deploy"
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null) || die "no upstream origin/$BRANCH"

# ---------- 关键判断：运行中的容器已经是远端最新版就直接退出 ----------
RUNNING=$(docker inspect -f '{{ index .Config.Labels "git_commit" }}' "$CONTAINER" 2>/dev/null || true)
if [[ -n "$RUNNING" && "$RUNNING" == "$REMOTE" ]]; then
  log "already deployed ${REMOTE:0:8}, skip"
  exit 0
fi

# ---------- 拉代码（分叉保护） ----------
LOCAL=$(git rev-parse HEAD)
if [[ "$LOCAL" != "$REMOTE" ]]; then
  if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
    die "local branch has diverged from origin/$BRANCH, manual fix required"
  fi
  log "update available: ${LOCAL:0:8} -> ${REMOTE:0:8}"
  git pull --ff-only
fi

# ---------- 部署（GIT_COMMIT 烤进镜像标签，供下轮比对） ----------
# 旧版本容器曾以 root 运行，数据卷里文件的属主是 root；
# 新版容器以 uid 10001 运行，部署前先修正卷属主（卷不存在时跳过，compose 会按镜像属主初始化）
VOL=$(docker volume ls -q --filter "label=com.docker.compose.volume=smsync-data" 2>/dev/null | head -1 || true)
if [[ -n "$VOL" ]]; then
  docker run --rm -v "$VOL:/data" alpine sh -c "chown -R 10001:10001 /data" \
    || log "warn: chown data volume failed"
fi

log "deploying ${REMOTE:0:8} (running: ${RUNNING:-<none>}) ..."
if GIT_COMMIT="$REMOTE" "${DC[@]}" up -d --build --remove-orphans; then
  printf '%s\n' "$REMOTE" > "$MARKER.tmp" && mv -f "$MARKER.tmp" "$MARKER"
  log "deploy finished (${REMOTE:0:8})"
else
  die "docker compose up failed, will retry next round"
fi
