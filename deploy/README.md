# deploy/ —— 部署与备份件

本目录放**不进主代码、换服务器要用**的部署件：systemd 单元 + 备份/恢复脚本。

> 📖 **完整的保姆级操作步骤**（部署、rclone 授权、双云盘备份、Ghost 备份、灾后恢复、
> 常见问题）见项目外的总教程：`桌面/服务器部署备份恢复-总教程.md`。
> 本文件只做速查清单。

## 文件清单

| 文件 | 作用 |
|---|---|
| `odds-bot.service` | 主守护进程（TG bot + 调度器）systemd 单元 |
| `backup.sh` | odds 备份：打包 .env/odds.db/report/data → 传多云盘 → 清超期 |
| `odds-backup.service` / `.timer` | odds 每日备份（北京 04:00） |
| `restore.sh` | odds 恢复：从云盘拉最新备份并解开（带覆盖护栏） |
| `ghost-backup.sh` | Ghost 备份：mysqldump + content volume + 配置 → 传多云盘 |
| `ghost-backup.service` / `.timer` | Ghost 每日备份（北京 04:30） |
| `ghost-restore.sh` | Ghost 恢复：灌回 MySQL + 还原 content + 重启容器 |
| `verify-backup.sh` | 备份完整性抽查：下载最新备份、试解压、校验关键文件 |
| `verify-backup.service` / `.timer` | 每周日 05:00 自动抽查 |

## 极简速查

```bash
# 拉代码后净化换行（Windows 编辑过的脚本）
cd ~/odds-bot && git pull origin main
dos2unix deploy/*.sh 2>/dev/null || sed -i 's/\r$//' deploy/*.sh && chmod +x deploy/*.sh

# 手动跑各备份
bash deploy/backup.sh          # odds → gdrive + pikpak
bash deploy/ghost-backup.sh    # ghost → gdrive + pikpak
bash deploy/verify-backup.sh   # 抽查完整性

# 装全部定时器
for u in odds-backup ghost-backup verify-backup; do
    sudo cp deploy/$u.service deploy/$u.timer /etc/systemd/system/
done
sudo systemctl daemon-reload
sudo systemctl enable --now odds-backup.timer ghost-backup.timer verify-backup.timer
systemctl list-timers 'odds-backup.timer' 'ghost-backup.timer' 'verify-backup.timer'
```

## 可调参数（脚本顶部或 .service 的 Environment=）

- `RCLONE_REMOTES`（默认 `gdrive pikpak`）：备份传哪些云盘，空格分隔
- `RETAIN_DAYS`（默认 14）：保留天数
- `REMOTE_DIR`：云盘目标文件夹（odds=`odds-bot-backups`，ghost=`ghost-backups`）
- Ghost 专用：`GHOST_DIR` / `MYSQL_CONTAINER` / `GHOST_VOLUME`
