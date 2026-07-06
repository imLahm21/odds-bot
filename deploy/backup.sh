#!/usr/bin/env bash
# 全局备份 → 打包 → 上传到【多个】云盘（rclone）→ 清理超期备份
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
# 依赖：sqlite3、tar、rclone（且已配好下述各 remote）
set -euo pipefail

# TG 通知：备份成/败推管理员对话框（读 .env 的 token + 管理员 chat_id，独立于 bot 进程）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/notify.sh
source "$SCRIPT_DIR/notify.sh"
notify_on_exit "odds 每日备份"

# ── 可调参数 ─────────────────────────────────────────────────────────────────
PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/odds-bot}"   # 项目根目录
# 多云盘：空格分隔的 rclone remote 名，逐个上传（双保险，一个挂了还有另一个）。
# 兼容旧单 remote 变量 RCLONE_REMOTE（若设了就用它，否则用默认的 gdrive pikpak）。
RCLONE_REMOTES="${RCLONE_REMOTES:-${RCLONE_REMOTE:-gdrive pikpak}}"
REMOTE_DIR="${REMOTE_DIR:-odds-bot-backups}"          # 各云盘上的目标文件夹
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

# ── 逐个云盘上传 + 清理超期 ──────────────────────────────────────────────────
# 单个 remote 失败不中断其它（一个云盘挂了还有另一个），末尾汇总成败。
upload_ok=0
upload_fail=0
for remote in $RCLONE_REMOTES; do
    if rclone copy "$ARCHIVE" "${remote}:${REMOTE_DIR}/" --stats-one-line; then
        echo "[ok] 已上传到 ${remote}:${REMOTE_DIR}/"
        upload_ok=$((upload_ok + 1))
        # 该云盘上清理超期备份
        rclone delete "${remote}:${REMOTE_DIR}/" \
            --min-age "${RETAIN_DAYS}d" --include "odds-backup_*.tar.gz" 2>/dev/null || \
            echo "[warn] ${remote} 云端清理失败（忽略，下次再清）"
    else
        echo "[error] 上传到 ${remote} 失败（其它云盘继续）"
        upload_fail=$((upload_fail + 1))
    fi
done

# 本地：同样只留 RETAIN_DAYS 天，避免服务器磁盘堆满
find "$LOCAL_KEEP_DIR" -name "odds-backup_*.tar.gz" \
    -mtime "+${RETAIN_DAYS}" -delete || true
echo "[ok] 已清理本地 >${RETAIN_DAYS} 天的旧备份"

if [ "$upload_ok" -eq 0 ]; then
    echo "[error] 所有云盘上传均失败！本地备份仍在 $ARCHIVE"; exit 1
fi
echo "[done] 备份完成 @ ${STAMP}（成功 ${upload_ok} 个云盘，失败 ${upload_fail} 个）"
