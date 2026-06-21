#!/usr/bin/env bash
#
# setup_env_watch.sh — 让 systemd 监听 .env 变化，保存即自动重启 odds-bot
#
# 解决的问题：改 .env 白名单后忘记手动重启，导致 bot 仍按旧白名单拒绝已授权用户。
# 原理：一个 systemd path 单元盯住 .env，文件一变就触发一次性 service 去 restart odds-bot。
#
# 用法（在服务器上）：
#   bash setup_env_watch.sh           # 安装并启用
#   bash setup_env_watch.sh --remove  # 卸载
#
# 前提：odds-bot.service 已存在并能用（见 README「备选：systemd」）。

set -euo pipefail

# .env 绝对路径（默认按本脚本所在目录推断；如目录不同可在此覆盖）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"

TARGET_SERVICE="odds-bot.service"
PATH_UNIT="/etc/systemd/system/odds-bot-env.path"
RESTART_UNIT="/etc/systemd/system/odds-bot-restart.service"

remove() {
    echo "==> 卸载 .env 监听"
    sudo systemctl disable --now odds-bot-env.path 2>/dev/null || true
    sudo rm -f "$PATH_UNIT" "$RESTART_UNIT"
    sudo systemctl daemon-reload
    echo "已移除 $PATH_UNIT 与 $RESTART_UNIT"
    exit 0
}

[[ "${1:-}" == "--remove" ]] && remove

if [[ ! -f "$ENV_FILE" ]]; then
    echo "错误：找不到 .env：$ENV_FILE" >&2
    echo "请在项目根目录运行本脚本，或用 ENV_FILE=/abs/path/.env bash setup_env_watch.sh 指定。" >&2
    exit 1
fi

echo "==> 监听目标 .env：$ENV_FILE"
echo "==> 触发重启服务：$TARGET_SERVICE"

# 1) path 单元：监听 .env 变化
sudo tee "$PATH_UNIT" >/dev/null <<EOF
[Unit]
Description=Watch odds-bot .env and restart on change

[Path]
# vim 改名覆盖写入一般触发 PathModified；若发现保存后未重启，改成 PathChanged 再 daemon-reload
PathModified=$ENV_FILE
Unit=odds-bot-restart.service

[Install]
WantedBy=multi-user.target
EOF

# 2) 一次性重启服务
sudo tee "$RESTART_UNIT" >/dev/null <<EOF
[Unit]
Description=Restart odds-bot after .env change

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart $TARGET_SERVICE
EOF

# 3) 启用
sudo systemctl daemon-reload
sudo systemctl enable --now odds-bot-env.path

echo
echo "==> 已启用，状态："
systemctl status odds-bot-env.path --no-pager || true

echo
echo "==> 自检（touch .env 看 odds-bot 是否自动重启）："
before="$(systemctl show "$TARGET_SERVICE" -p ActiveEnterTimestamp --value || true)"
touch "$ENV_FILE"
sleep 2
after="$(systemctl show "$TARGET_SERVICE" -p ActiveEnterTimestamp --value || true)"
echo "重启时间  前: $before"
echo "重启时间  后: $after"
if [[ "$before" != "$after" ]]; then
    echo "✅ 成功：保存 .env 会自动重启 odds-bot，新白名单立即生效。"
else
    echo "⚠️  时间未变化。把 $PATH_UNIT 里的 PathModified 改成 PathChanged，再："
    echo "    sudo systemctl daemon-reload && sudo systemctl restart odds-bot-env.path"
fi

echo
echo "备用命令："
echo "  反复存盘被限流报 start-limit：  sudo systemctl reset-failed odds-bot"
echo "  临时关闭监听：                  sudo systemctl disable --now odds-bot-env.path"
echo "  彻底卸载：                      bash setup_env_watch.sh --remove"
