#!/usr/bin/env bash
# 从云盘拉取 Ghost 备份并恢复 —— 换机 / 灾后还原
#
# 用法：
#   bash deploy/ghost-restore.sh --list                 # 列出云端有哪些 Ghost 备份
#   bash deploy/ghost-restore.sh                        # 拉最新一份并恢复
#   bash deploy/ghost-restore.sh 2026-07-05_043000       # 恢复指定时间戳那份
#
# 前提：目标机已 docker compose up 起了 Ghost 那套（mysql 容器在跑），rclone 已配好。
# 恢复动作：① 灌回 MySQL 全库 ② 还原 content volume ③ 释出 compose/Caddyfile 供参考
set -euo pipefail

GHOST_DIR="${GHOST_DIR:-/home/ubuntu/ghost-paywall-blog}"
MYSQL_CONTAINER="${MYSQL_CONTAINER:-ghost-paywall-blog-mysql-1}"
GHOST_VOLUME="${GHOST_VOLUME:-ghost-paywall-blog_ghost-content}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"          # 从哪个云盘拉（默认 gdrive，可设 pikpak）
REMOTE_DIR="${REMOTE_DIR:-ghost-backups}"
WORK_DIR="${WORK_DIR:-/tmp/ghost-restore}"
REMOTE_PATH="${RCLONE_REMOTE}:${REMOTE_DIR}"

if [ "${1:-}" = "--list" ]; then
    echo "云端 ${REMOTE_PATH}/ 的 Ghost 备份："
    rclone lsf "${REMOTE_PATH}/" --include "ghost-backup_*.tar.gz" | sort
    exit 0
fi

# 选定备份文件
if [ -n "${1:-}" ]; then
    FILE="ghost-backup_${1}.tar.gz"
else
    FILE="$(rclone lsf "${REMOTE_PATH}/" --include 'ghost-backup_*.tar.gz' | sort | tail -n1)"
    [ -n "$FILE" ] || { echo "[error] 云端无 Ghost 备份"; exit 1; }
fi
echo "[info] 准备还原：$FILE"

# 下载 + 解开总包
rm -rf "$WORK_DIR"; mkdir -p "$WORK_DIR"
rclone copy "${REMOTE_PATH}/${FILE}" "$WORK_DIR/" --stats-one-line
tar xzf "$WORK_DIR/$FILE" -C "$WORK_DIR"
echo "[ok] 已下载并解开到 $WORK_DIR"

# 确认 mysql 容器在跑
if ! docker ps --format '{{.Names}}' | grep -q "^${MYSQL_CONTAINER}$"; then
    echo "[error] mysql 容器 $MYSQL_CONTAINER 未运行。请先在 $GHOST_DIR 里"
    echo "        docker compose up -d 起服务，再跑本恢复脚本。"; exit 1
fi

# ① 灌回 MySQL
if [ -f "$WORK_DIR/mysql-all.sql" ]; then
    ROOT_PW="$(docker exec "$MYSQL_CONTAINER" printenv MYSQL_ROOT_PASSWORD)"
    echo "[..] 正在导入 MySQL 全库（会覆盖现有同名库）…"
    docker exec -i -e MYSQL_PWD="$ROOT_PW" "$MYSQL_CONTAINER" \
        mysql -u root < "$WORK_DIR/mysql-all.sql"
    echo "[ok] MySQL 已恢复"
else
    echo "[warn] 备份里没有 mysql-all.sql，跳过数据库恢复"
fi

# ② 还原 content volume
if [ -f "$WORK_DIR/ghost-content.tar.gz" ]; then
    VOL_PATH="/var/lib/docker/volumes/${GHOST_VOLUME}/_data"
    sudo mkdir -p "$VOL_PATH"
    sudo tar xzf "$WORK_DIR/ghost-content.tar.gz" -C "$VOL_PATH"
    echo "[ok] content 目录已还原到 $VOL_PATH"
else
    echo "[warn] 备份里没有 ghost-content.tar.gz，跳过 content 还原"
fi

# ③ 部署配置释出到 WORK_DIR 供参考（不自动覆盖现网 compose，避免误伤）
if [ -f "$WORK_DIR/deploy-config.tar.gz" ]; then
    mkdir -p "$WORK_DIR/deploy-config"
    tar xzf "$WORK_DIR/deploy-config.tar.gz" -C "$WORK_DIR/deploy-config"
    echo "[ok] 部署配置已释出到 $WORK_DIR/deploy-config/（compose/Caddyfile，供对照）"
fi

echo "[..] 重启 Ghost 让改动生效…"
docker restart "$MYSQL_CONTAINER" >/dev/null 2>&1 || true
# ghost 容器名按约定推断（把 mysql 换成 ghost）
GHOST_CT="${MYSQL_CONTAINER/mysql/ghost}"
docker restart "$GHOST_CT" >/dev/null 2>&1 || \
    echo "[warn] 未能自动重启 ghost 容器，请手动 docker compose restart"
echo "[done] Ghost 恢复完成。浏览器打开站点确认文章/会员/图片是否齐全。"
