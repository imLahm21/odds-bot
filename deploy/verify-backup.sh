#!/usr/bin/env bash
# 备份完整性抽查 —— 下载云端【最新】备份，试解压 + 校验关键文件是否齐全
#
# 备份最怕「以为在备、真要用时才发现是坏的」。本脚本定期把云端最新备份拉下来，
# 实际解压一遍并检查里面该有的文件在不在，验完即删临时文件。任一项不合格则退出码
# 非 0，配合 systemd timer 每周自动跑，坏了能及早发现。
#
# 用法：
#   bash deploy/verify-backup.sh            # 验所有云盘的 odds + ghost 最新备份
#   由 verify-backup.timer 每周自动调用
#
# 依赖：rclone、tar、gzip
set -uo pipefail   # 注意：不加 -e，单项失败要继续验其它项，末尾统一判定

RCLONE_REMOTES="${RCLONE_REMOTES:-gdrive pikpak}"
ODDS_REMOTE_DIR="${ODDS_REMOTE_DIR:-odds-bot-backups}"
GHOST_REMOTE_DIR="${GHOST_REMOTE_DIR:-ghost-backups}"
WORK_DIR="${WORK_DIR:-/tmp/verify-backup}"

fail=0
rm -rf "$WORK_DIR"; mkdir -p "$WORK_DIR"

# verify_one <remote> <remote_dir> <文件名前缀> <期望的关键文件...>
# 拉最新一份、解压、检查关键文件存在且非空。
verify_one() {
    local remote="$1" dir="$2" prefix="$3"; shift 3
    local expects=("$@")
    local tag="${remote}:${dir}"

    # 找云端最新一份（时间戳命名，字典序最大即最新）
    local file
    file="$(rclone lsf "${remote}:${dir}/" --include "${prefix}_*.tar.gz" 2>/dev/null \
            | sort | tail -n1)"
    if [ -z "$file" ]; then
        echo "[FAIL] $tag：云端没有 ${prefix}_*.tar.gz 备份"
        fail=$((fail + 1)); return
    fi

    # 下载
    local sub="$WORK_DIR/${remote}_${prefix}"
    mkdir -p "$sub"
    if ! rclone copy "${remote}:${dir}/${file}" "$sub/" 2>/dev/null; then
        echo "[FAIL] $tag：下载 $file 失败"
        fail=$((fail + 1)); return
    fi
    local arc="$sub/$file"

    # gzip 完整性 + tar 能否解开
    if ! gzip -t "$arc" 2>/dev/null; then
        echo "[FAIL] $tag：$file gzip 校验失败（文件损坏）"
        fail=$((fail + 1)); return
    fi
    if ! tar xzf "$arc" -C "$sub" 2>/dev/null; then
        echo "[FAIL] $tag：$file 解压失败"
        fail=$((fail + 1)); return
    fi

    # 检查关键文件都在且非空
    local missing=0
    for want in "${expects[@]}"; do
        # 用 find 支持通配（如 odds.db、report 目录）
        if ! find "$sub" -name "$want" 2>/dev/null | grep -q .; then
            echo "[FAIL] $tag：缺少关键内容「$want」"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -gt 0 ]; then
        fail=$((fail + 1)); return
    fi

    local sz; sz="$(du -h "$arc" | cut -f1)"
    echo "[OK]   $tag：$file（$sz）解压正常，关键文件齐全"
}

echo "===== 备份完整性抽查 $(date '+%Y-%m-%d %H:%M') ====="
for remote in $RCLONE_REMOTES; do
    # odds 备份：至少要有 .env 和 odds.db
    verify_one "$remote" "$ODDS_REMOTE_DIR" "odds-backup" ".env" "odds.db"
    # ghost 备份：至少要有 mysql dump（content 可能因某些部署缺失，故不强制）
    verify_one "$remote" "$GHOST_REMOTE_DIR" "ghost-backup" "mysql-all.sql"
done

rm -rf "$WORK_DIR"
if [ "$fail" -eq 0 ]; then
    echo "[done] 全部备份验证通过 ✅"
    exit 0
else
    echo "[done] ⚠️ 有 $fail 项验证未通过，请检查上面的 [FAIL] 行！"
    exit 1
fi
