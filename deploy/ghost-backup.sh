#!/usr/bin/env bash
# Ghost（Docker + MySQL）全站备份 → 打包 → 上传多个云盘 → 清理超期
#
# 为什么不能直接拷文件夹：Ghost 数据在 MySQL 容器里，运行中直接拷 mysql 数据目录
# 会拿到写一半的不一致状态，恢复大概率坏库。故 MySQL 必须用 mysqldump 导出一致快照。
#
# 备份三样（缺一不可完整还原）：
#   1. MySQL 全库 dump（文章/会员/设置全在这）—— docker exec 进 mysql 容器导出
#   2. ghost-content volume（图片/主题/上传文件）—— 打包 volume 目录
#   3. 部署配置（docker-compose.yml + Caddyfile）—— 换机重建用
#
# 用法：
#   bash deploy/ghost-backup.sh
#   由 systemd timer 每天调用（见 ghost-backup.timer）
#
# 依赖：docker、tar、rclone（已配好各 remote）
set -euo pipefail

# ── 可调参数（按 docker ps / compose 实际情况，一般不用改）──────────────────
GHOST_DIR="${GHOST_DIR:-/home/ubuntu/ghost-paywall-blog}"          # compose 所在目录
MYSQL_CONTAINER="${MYSQL_CONTAINER:-ghost-paywall-blog-mysql-1}"   # mysql 容器名
GHOST_VOLUME="${GHOST_VOLUME:-ghost-paywall-blog_ghost-content}"   # content volume 名
RCLONE_REMOTES="${RCLONE_REMOTES:-gdrive pikpak}"                  # 多云盘，空格分隔
REMOTE_DIR="${REMOTE_DIR:-ghost-backups}"                          # 各云盘目标文件夹
LOCAL_KEEP_DIR="${LOCAL_KEEP_DIR:-/home/ubuntu/ghost-backups}"     # 本地暂存
RETAIN_DAYS="${RETAIN_DAYS:-14}"

STAMP="$(date +%Y-%m-%d_%H%M%S)"
STAGE="$(mktemp -d)"                       # 临时组装目录
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$LOCAL_KEEP_DIR"

# ── 1. MySQL 一致快照（从容器内 dump，密码取自容器自己的环境变量）─────────────
# 从运行中的容器读 MYSQL_ROOT_PASSWORD，脚本里不留明文密码。
ROOT_PW="$(docker exec "$MYSQL_CONTAINER" printenv MYSQL_ROOT_PASSWORD 2>/dev/null || true)"
if [ -z "$ROOT_PW" ]; then
    echo "[error] 无法从容器 $MYSQL_CONTAINER 读到 MYSQL_ROOT_PASSWORD。"
    echo "        确认容器名对不对：docker ps"; exit 1
fi
echo "[..] 正在导出 MySQL 全库…"
# --single-transaction：InnoDB 下不锁表拿一致快照；--all-databases 连 ghost 库带用户库全导
docker exec -e MYSQL_PWD="$ROOT_PW" "$MYSQL_CONTAINER" \
    mysqldump -u root --single-transaction --routines --triggers --events \
    --all-databases > "$STAGE/mysql-all.sql"
sql_size="$(du -h "$STAGE/mysql-all.sql" | cut -f1)"
echo "[ok] MySQL 导出完成（$sql_size）"

# ── 2. ghost-content volume（图片/主题）──────────────────────────────────────
VOL_PATH="/var/lib/docker/volumes/${GHOST_VOLUME}/_data"
if [ -d "$VOL_PATH" ]; then
    # volume 属 root，用 sudo 打包；-C 让归档内路径干净
    sudo tar czf "$STAGE/ghost-content.tar.gz" -C "$VOL_PATH" .
    echo "[ok] content 目录已打包（$(sudo du -sh "$VOL_PATH" | cut -f1)）"
else
    echo "[warn] 未找到 content volume：$VOL_PATH（跳过，仅备数据库）"
fi

# ── 3. 部署配置（compose + Caddyfile）────────────────────────────────────────
if [ -d "$GHOST_DIR" ]; then
    # 只收配置类文件，不收 volume 挂载点
    sudo tar czf "$STAGE/deploy-config.tar.gz" -C "$GHOST_DIR" \
        $(cd "$GHOST_DIR" && ls docker-compose.yml Caddyfile .env 2>/dev/null) \
        2>/dev/null || echo "[warn] 部分配置文件缺失，已尽力打包"
    echo "[ok] 部署配置已打包"
fi

# ── 组装成一个总包 ───────────────────────────────────────────────────────────
ARCHIVE="$LOCAL_KEEP_DIR/ghost-backup_${STAMP}.tar.gz"
sudo tar czf "$ARCHIVE" -C "$STAGE" .
sudo chown "$(id -u):$(id -g)" "$ARCHIVE"      # 交回当前用户，便于 rclone 读取
echo "[ok] 已打包总备份：$ARCHIVE（$(du -h "$ARCHIVE" | cut -f1)）"

# ── 逐个云盘上传 + 清理超期 ──────────────────────────────────────────────────
upload_ok=0
for remote in $RCLONE_REMOTES; do
    if rclone copy "$ARCHIVE" "${remote}:${REMOTE_DIR}/" --stats-one-line; then
        echo "[ok] 已上传到 ${remote}:${REMOTE_DIR}/"
        upload_ok=$((upload_ok + 1))
        rclone delete "${remote}:${REMOTE_DIR}/" \
            --min-age "${RETAIN_DAYS}d" --include "ghost-backup_*.tar.gz" 2>/dev/null || \
            echo "[warn] ${remote} 云端清理失败（忽略）"
    else
        echo "[error] 上传到 ${remote} 失败（其它云盘继续）"
    fi
done

find "$LOCAL_KEEP_DIR" -name "ghost-backup_*.tar.gz" \
    -mtime "+${RETAIN_DAYS}" -delete || true
echo "[ok] 已清理本地 >${RETAIN_DAYS} 天的旧备份"

if [ "$upload_ok" -eq 0 ]; then
    echo "[error] 所有云盘上传均失败！本地备份仍在 $ARCHIVE"; exit 1
fi
echo "[done] Ghost 备份完成 @ ${STAMP}（成功上传 ${upload_ok} 个云盘）"
