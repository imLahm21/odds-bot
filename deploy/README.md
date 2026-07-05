# 部署 & 备份

本目录放**不进主代码、但换服务器要用**的部署件：systemd 单元 + 每日 Google Drive 备份。

## 文件一览

| 文件 | 作用 |
|---|---|
| `odds-bot.service` | 主守护进程（TG bot + 调度器）的 systemd 单元 |
| `backup.sh` | 全局备份脚本：打包 `.env`/`odds.db`/`report`/`data` → 上传 Drive → 清理超期 |
| `odds-backup.service` | 备份任务本体（oneshot） |
| `odds-backup.timer` | 每天北京时间 04:00 触发备份 |

> 路径默认按 `/home/ubuntu/odds-bot`、用户 `ubuntu`。换服务器改各文件里的路径与 `User=` 即可。

---

## 一、跑起主服务

```bash
cd ~/odds-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt          # 依赖
# .env 与 odds.db 从备份恢复（新装则自建 .env、db 首次运行自动建）

sudo cp deploy/odds-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now odds-bot
journalctl -u odds-bot -f                 # 看日志，确认 "Telegram bot 启动"
```

---

## 二、配置每日备份到 Google Drive

### 1. 装 rclone

```bash
sudo -v ; curl https://rclone.org/install.sh | sudo bash
rclone version
```

### 2. 授权连上你的 Google Drive（关键：服务器无浏览器怎么办）

服务器通常没有图形浏览器，用 rclone 的 **headless 授权**：

```bash
rclone config
```

依次选：
- `n`（新建 remote）→ 名字填 **`gdrive`**（必须与脚本里 `RCLONE_REMOTE` 一致）
- storage 类型选 **`drive`**（Google Drive，输入对应编号）
- `client_id` / `client_secret`：直接回车留空（用 rclone 默认，够用）
- scope 选 `1`（完整访问）或 `2`（仅 rclone 建的文件，更安全，推荐 `2`）
- `root_folder_id` / `service_account`：回车跳过
- **Edit advanced config? → `n`**
- **Use web browser to automatically authenticate? → `n`**（⚠️服务器无浏览器，必须选 n）
- 此时 rclone 给出一条 `rclone authorize "drive" ...` 命令。**复制它，到你自己有浏览器的电脑上**（本地也装个 rclone）运行，浏览器弹出 Google 登录 → 授权 → 终端吐出一段 token
- 把那段 token 粘回服务器提示处
- Configure this as a Shared Drive? → `n`
- 确认 `y` 保存，`q` 退出

验证：
```bash
rclone lsd gdrive:            # 能列出 Drive 根目录的文件夹即成功
```

### 3. 先手动跑一次备份，确认全链路通

```bash
bash ~/odds-bot/deploy/backup.sh
rclone ls gdrive:odds-bot-backups/     # 应看到 odds-backup_日期.tar.gz
```

### 4. 装定时器（每天北京时间 04:00 自动跑）

```bash
sudo cp deploy/odds-backup.service /etc/systemd/system/
sudo cp deploy/odds-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now odds-backup.timer

systemctl list-timers odds-backup.timer   # 看 NEXT 那列确认下次触发时间
```

> 定时器用 systemd（非 crontab）：`Persistent=true` 保证机器在 04:00 恰好关机/休眠时，
> 开机后会补跑一次错过的备份，不会漏。

---

## 三、日常查验

```bash
systemctl list-timers odds-backup.timer        # 下次何时跑
journalctl -u odds-backup.service -n 30         # 最近一次备份日志
rclone ls gdrive:odds-bot-backups/              # 云端现有备份（应只留最近 14 天）
sudo systemctl start odds-backup.service        # 立即手动触发一次
```

## 四、从备份恢复（新服务器）

```bash
git clone https://github.com/imLahm21/odds-bot.git && cd odds-bot
rclone copy gdrive:odds-bot-backups/odds-backup_某日期.tar.gz .
tar xzf odds-backup_某日期.tar.gz -C .    # 解出 .env / odds.db / report / data
chmod 600 .env                            # 密钥文件收紧权限
# 然后回到「一、跑起主服务」
```

## 可调参数

`backup.sh` 顶部或 `odds-backup.service` 的 `Environment=` 可改：
- `RETAIN_DAYS`（默认 14）：云端 + 本地保留天数
- `RCLONE_REMOTE`（默认 gdrive）：rclone remote 名
- `REMOTE_DIR`（默认 odds-bot-backups）：Drive 上的目标文件夹
