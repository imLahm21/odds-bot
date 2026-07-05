#!/usr/bin/env bash
# 从 Google Drive 拉取备份并恢复 —— 新服务器一条命令还原数据
#
# 用法：
#   bash deploy/restore.sh                 # 自动拉云端【最新】一份备份并解开
#   bash deploy/restore.sh 2026-07-05_040001   # 指定某个备份的时间戳还原
#   bash deploy/restore.sh --list          # 只列出云端有哪些备份，不还原
#
# 前提：已装 rclone 且配好名为 gdrive 的 remote（见 deploy/README.md 二.1~2）。
# 恢复内容：.env / odds.db / report/ / data/（覆盖当前目录同名项）。
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/odds-bot}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"
REMOTE_DIR="${REMOTE_DIR:-odds-bot-backups}"
WORK_DIR="${WORK_DIR:-/tmp/odds-restore}"

REMOTE_PATH="${RCLONE_REMOTE}:${REMOTE_DIR}"

# ── --list：只列出云端备份 ───────────────────────────────────────────────────
if [ "${1:-}" = "--list" ]; then
    echo "云端 ${REMOTE_PATH}/ 的备份："
    rclone lsf "${REMOTE_PATH}/" --include "odds-backup_*.tar.gz" | sort
    exit 0
fi

# ── 选定要还原的备份文件名 ───────────────────────────────────────────────────
if [ -n "${1:-}" ]; then
    FILE="odds-backup_${1}.tar.gz"          # 指定时间戳
else
    # 不带参数：取名字排序最大的（时间戳命名，字典序最大即最新）
    FILE="$(rclone lsf "${REMOTE_PATH}/" --include 'odds-backup_*.tar.gz' \
            | sort | tail -n1)"
    [ -n "$FILE" ] || { echo "[error] 云端没有任何备份文件"; exit 1; }
fi
echo "[info] 准备还原：$FILE"

# ── 下载 ─────────────────────────────────────────────────────────────────────
mkdir -p "$WORK_DIR"
rclone copy "${REMOTE_PATH}/${FILE}" "$WORK_DIR/" --stats-one-line
ARCHIVE="$WORK_DIR/$FILE"
[ -f "$ARCHIVE" ] || { echo "[error] 下载失败：$ARCHIVE 不存在"; exit 1; }
echo "[ok] 已下载：$ARCHIVE（$(du -h "$ARCHIVE" | cut -f1)）"

# ── 安全护栏：若当前目录已有 odds.db，先另存旧库再覆盖 ────────────────────────
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"
if [ -f odds.db ]; then
    BAK="odds.db.before-restore.$(date +%s)"
    cp odds.db "$BAK"
    echo "[warn] 检测到已有 odds.db，已另存为 $BAK 再覆盖（防误删现有数据）"
fi

# ── 解开到项目目录 ───────────────────────────────────────────────────────────
tar xzf "$ARCHIVE" -C "$PROJECT_DIR"
[ -f .env ] && chmod 600 .env               # 密钥文件收紧权限
echo "[ok] 已解开到 $PROJECT_DIR"
echo "[done] 恢复完成。接着：装依赖 + 起服务（见 deploy/README.md 一）。"
