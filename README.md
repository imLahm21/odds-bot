# odds-bot — 足球赔率轮询抓取与精算后台

基于 **API-Football（Pro 套餐）** 的全自动轮询守护进程。定时抓取关注联赛的欧赔/亚盘盘口，
按时间序列存入 SQLite，供精算 SOP（见 `CLAUDE.md`）人工分析使用。

> 旧的交互式查询脚本 `main.py`（基于 The Odds API）仍保留，与新守护进程互不影响。

---

## 架构

```
bot/
├── config.py       # 联赛清单(league_id+season)、关注庄家、轮询间隔、节点定义——改这里即可增删联赛
├── api_client.py   # API-Football 请求层：key 轮换 + 429/401 重试
├── db.py           # SQLite：建表 / 批量插入 / WAL / 去重
├── parser.py       # JSON→行解析 + 凯利计算（亚盘文本解析含完整 1/4 盘）
├── scheduler.py    # apscheduler 三档定时任务 A/B/C
└── daemon.py       # 入口：初始化→拉赛程→启动调度器
probe.py            # 阶段0 探针：实测 API 真实 JSON（开发用，部署不需要）
```

### 数据库两张表

- `fixtures`：赛程基本面（对阵、开球时间、联赛、状态）
- `odds_history`：带时间戳的盘口快照（欧赔 + 亚盘 + 凯利），是重建 SOP 10 节点的核心

### 三档定时任务

| 任务 | 频率 | 动作 |
|------|------|------|
| A | 每日 01:07 | 拉关注联赛未来 14 天赛程 → `fixtures` |
| B | 每 2 小时 | 抓未来 3 天比赛最新赔率 → `odds_history` |
| C | 每 15 分钟 | 仅抓开球前 2h 内的比赛（临场高频）|

---

## 服务器部署（甲骨文 ARM）

### 1. 拉代码

```bash
cd ~
gh auth login                  # 若未装：sudo apt install gh
git clone https://github.com/imLahm21/odds-bot.git
cd odds-bot
```

### 2. 装依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置 API key（关键：.env 不在 git 里，需手动建）

```bash
cat > .env <<'EOF'
APIFOOTBALL_KEY=你的API-Football密钥
EOF
```

> `odds.db` 会在首次运行时自动创建，无需手动建。

### 4. 后台保活（tmux）

```bash
tmux new -s bot
source venv/bin/activate
python -m bot.daemon
# 按 Ctrl+b 然后 d 挂起；关掉本地电脑后进程继续在云端运行
```

重新查看：`tmux attach -t bot`

### 备选：systemd（开机自启、崩溃自动重启）

```ini
# /etc/systemd/system/odds-bot.service
[Unit]
Description=Odds polling daemon
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/odds-bot
ExecStart=/home/ubuntu/odds-bot/venv/bin/python -m bot.daemon
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now odds-bot
journalctl -u odds-bot -f      # 看日志
```

---

## 增删联赛

只改 `bot/config.py` 的 `WATCH_LEAGUES`（key=league_id，value=(中文名, season)）。
需要新联赛的 ID 时，跑 `python probe.py leagues` 实测查出。

## 查数据

```bash
sqlite3 odds.db "SELECT count(*) FROM odds_history;"
sqlite3 odds.db "SELECT home_team, away_team, commence_utc FROM fixtures ORDER BY commence_utc LIMIT 10;"
```

## 实测确认的关键映射

| 项 | 值 |
|---|---|
| Pinnacle / Bet365 bookmaker id | 4 / 8 |
| 欧赔(Match Winner) / 亚盘(Asian Handicap) bet id | 1 / 4 |
| 账号额度 | Pro，每日 7500 / 每分钟 300 |
| 亚盘 | 含完整 1/4 盘（+0.75/+1.25 等），主队视角让球数存 `handicap`（负=主队受让）|
