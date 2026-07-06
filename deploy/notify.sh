#!/usr/bin/env bash
# TG 通知助手 —— 备份脚本 source 它，把成败结果推给管理员对话框。
#
# 为什么独立成文件：backup.sh / ghost-backup.sh / verify-backup.sh 都是【独立进程】
# （systemd timer 调用），跟常驻的 bot 进程分离，不能直接调 Python。最省心的做法是
# 各脚本 source 本文件，用 curl 直接打 Telegram sendMessage —— 完全不耦合 bot 进程，
# bot 挂着与否都能发。token 与管理员 chat_id 从项目 .env 读（与 bot 同源，不另配）。
#
# 用法（在备份脚本顶部、set -e 之后）：
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$SCRIPT_DIR/notify.sh"
#   notify_on_exit "odds 每日备份"     # 装 EXIT 陷阱：正常退出推✅、非0退出推❌
# 或手动：
#   tg_notify "✅ 备份完成"            # 直接发一条
#
# 读取的 .env 变量：
#   TELEGRAM_BOT_TOKEN         必填，机器人 token
#   TELEGRAM_ADMIN_CHAT_IDS    管理员 chat_id（逗号分隔，只推这些人）；
#                              未配则回退 TELEGRAM_ALLOWED_CHAT_IDS（与 bot 权限退化一致）
#   BACKUP_NOTIFY             可选，设为 0 关闭通知（默认开）

# 定位项目根的 .env（notify.sh 在 deploy/ 下，根在其上一级）
_NOTIFY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_ENV_FILE="${ENV_FILE:-${_NOTIFY_DIR}/../.env}"

# 从 .env 取一个变量值（不 source 整个 .env，避免把一堆密钥灌进当前 shell 环境）。
# 取「最后一次出现」的赋值（与 dotenv 覆盖语义一致），去掉可能的引号。
_env_get() {
    local key="$1"
    [ -f "$_ENV_FILE" ] || return 0
    local line
    line="$(grep -E "^[[:space:]]*${key}=" "$_ENV_FILE" 2>/dev/null | tail -n1)"
    [ -n "$line" ] || return 0
    local val="${line#*=}"
    val="${val%\"}"; val="${val#\"}"        # 去掉成对双引号
    val="${val%\'}"; val="${val#\'}"        # 去掉成对单引号
    printf '%s' "$val"
}

# tg_notify <消息文本> —— 推给所有管理员。静默失败（通知失败绝不该让备份脚本退非0）。
tg_notify() {
    local text="$1"
    [ "${BACKUP_NOTIFY:-1}" = "0" ] && return 0

    local token targets
    token="$(_env_get TELEGRAM_BOT_TOKEN)"
    targets="$(_env_get TELEGRAM_ADMIN_CHAT_IDS)"
    [ -n "$targets" ] || targets="$(_env_get TELEGRAM_ALLOWED_CHAT_IDS)"
    if [ -z "$token" ] || [ -z "$targets" ]; then
        echo "[notify] 未配置 TELEGRAM_BOT_TOKEN / 管理员 chat_id，跳过 TG 通知" >&2
        return 0
    fi

    # 主机名 + 时间前缀，便于多机/回溯定位
    local host stamp full
    host="$(hostname 2>/dev/null || echo '?')"
    stamp="$(date '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || date)"
    full="[$host $stamp]"$'\n'"$text"

    # 逗号分隔的多个 chat_id 逐个发
    local IFS=','
    for cid in $targets; do
        cid="$(printf '%s' "$cid" | tr -d '[:space:]')"
        [ -n "$cid" ] || continue
        curl -s -m 20 -o /dev/null \
            "https://api.telegram.org/bot${token}/sendMessage" \
            --data-urlencode "chat_id=${cid}" \
            --data-urlencode "text=${full}" \
            --data-urlencode "disable_web_page_preview=true" \
            || echo "[notify] 推送到 ${cid} 失败（忽略）" >&2
    done
}

# notify_on_exit <任务名> —— 装 EXIT 陷阱：脚本结束时按退出码自动推成/败。
# 捕获既有 EXIT trap（如 mktemp 的 rm -rf 清理）并串联，不覆盖。
notify_on_exit() {
    local task="$1"
    _NOTIFY_TASK="$task"
    # 保留已有 EXIT trap 命令，追加自己的，避免覆盖脚本原有清理逻辑
    local prev
    prev="$(trap -p EXIT | sed "s/^trap -- '//; s/' EXIT$//")"
    if [ -n "$prev" ]; then
        trap "${prev}; _notify_exit_handler" EXIT
    else
        trap "_notify_exit_handler" EXIT
    fi
}

_notify_exit_handler() {
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        tg_notify "✅ ${_NOTIFY_TASK:-任务} 成功完成。"
    else
        tg_notify "❌ ${_NOTIFY_TASK:-任务} 失败（退出码 ${rc}）。请上服务器看日志：journalctl -u <服务名> 或对应 .log。"
    fi
    return "$rc"
}
