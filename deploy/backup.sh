#!/usr/bin/env bash
# 全局备份 → 打包 → 上传 Google Drive（rclone）→ 清理超期备份
#
# 备份内容（只备 git 里没有、丢了不可恢复的）：
#   .env       所有 API 密钥/LLM 端点/Ghost 凭证（gitignore，最关键）
#   odds.db    历史盘口快照 + 关注配置 + 端点开关 + 访客额度
#   report/    归档的精算/复盘报告
#   data/      抓取的 CSV 快照（可选，能重抓，但一并备省事）
#
# 用法：
#   bash deploy/backup.sh              # 手动跑一次
#   由 systemd timer 每天北京时间 04:00 自动调用（见 odds-backup.*）
#
# 依赖：sqlite3、tar、rclone（且已配好名为 gdrive 的 remote）
set -euo pipefail

# ── 可调参数 ─────────────────────────────────────────────────────────────────
PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/odds-bot}"   # 项目根目录
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"              # rclone remote 名
REMOTE_DIR="${REMOTE_DIR:-odds-bot-backups}"          # Drive 上的目标文件夹
LOCAL_KEEP_DIR="${LOCAL_KEEP_DIR:-/home/ubuntu/odds-backups}"  # 本地暂存目录
RETAIN_DAYS="${RETAIN_DAYS:-14}"                      # 云端 + 本地保留天数

# ── 打包 ─────────────────────────────────────────────────────────────────────
cd "$PROJECT_DIR"
mkdir -p "$LOCAL_KEEP_DIR"

STAMP="$(date +%Y-%m-%d_%H%M%S)"
ARCHIVE="$LOCAL_KEEP_DIR/odds-backup_${STAMP}.tar.gz"

# SQLite 落盘：把 WAL 合并进主库，确保备份到的是一致快照（不停机也安全）
if [ -f odds.db ]; then
    sqlite3 odds.db "PRAGMA wal_checkpoint(TRUNCATE);" || \
        echo "[warn] wal_checkpoint 失败，仍继续打包（可能是库正被写）"
fi

# 只打包存在的目标（data/ 可能不存在也不报错）
TARGETS=()
for p in .env odds.db report data; do
    [ -e "$p" ] && TARGETS+=("$p")
done
if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "[error] 没有可备份的目标（.env/odds.db/report/data 均不存在）"; exit 1
fi

tar czf "$ARCHIVE" "${TARGETS[@]}"
echo "[ok] 已打包：$ARCHIVE（$(du -h "$ARCHIVE" | cut -f1)）"

# ── 上传 Google Drive ────────────────────────────────────────────────────────
rclone copy "$ARCHIVE" "${RCLONE_REMOTE}:${REMOTE_DIR}/" --stats-one-line
echo "[ok] 已上传到 ${RCLONE_REMOTE}:${REMOTE_DIR}/"

# ── 清理超期备份（云端 + 本地）───────────────────────────────────────────────
# 云端：删 Drive 上修改时间早于 RETAIN_DAYS 的备份文件
rclone delete "${RCLONE_REMOTE}:${REMOTE_DIR}/" \
    --min-age "${RETAIN_DAYS}d" --include "odds-backup_*.tar.gz" || \
    echo "[warn] 云端清理失败（忽略，下次再清）"
echo "[ok] 已清理云端 >${RETAIN_DAYS} 天的旧备份"

# 本地：同样只留 RETAIN_DAYS 天，避免服务器磁盘堆满
find "$LOCAL_KEEP_DIR" -name "odds-backup_*.tar.gz" \
    -mtime "+${RETAIN_DAYS}" -delete || true
echo "[ok] 已清理本地 >${RETAIN_DAYS} 天的旧备份"

echo "[done] 备份完成 @ ${STAMP}"
