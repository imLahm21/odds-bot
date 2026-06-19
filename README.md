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
| A | 每日 02:00 / 14:00 | 拉关注联赛未来 14 天赛程 → `fixtures` |
| B | 每 1 小时 | 抓未来 4 天比赛最新赔率 → `odds_history`（覆盖 -72h 初盘①）|
| C | 每 15 分钟 | 仅抓开球前 2h 内的比赛（临场高频）|

> 关注的联赛/庄家存在数据库的 `watched_leagues` / `watched_bookmakers` 表，
> 由 Telegram bot 实时开关，调度器每次抓取时读取——点完即时生效，无需重启。

### Telegram bot（可选）

配置了 `TELEGRAM_BOT_TOKEN` 时，守护进程会同时跑一个 TG bot，用内联按钮实时操控：

| 命令 | 作用 |
|------|------|
| `/leagues` | 联赛开关面板（点按钮 ✅启用/⬜停用）|
| `/bookmakers` | 庄家开关面板 |
| `/add <id> <season> [名称]` | 按 league_id 新增关注联赛 |
| `/remove <id>` | 删除关注联赛 |
| `/status` | 当前启用了哪些联赛/庄家 |
| `/fixtures` | 过去 3 天 ~ 未来 3 天赛程（✅已开赛可 `/review` / 🔵未来可 `/analyze`）|
| `/coverage <fixture_id>` | 看某场数据采集进度：10 节点抓了几个、缺哪些、各节点快照×庄家数、距开球时长 |
| `/odds <fixture_id>` | 某场 Pinnacle/Bet365 最新盘口 |
| `/export <fixture_id>` | 导出某场全部盘口快照为 CSV 文件 |
| `/analyze <fixture_id>` | 先看基本面+盘口走势，再点按钮选**预设**或**自定义侧重**跑 SOP 预测（分两步，避免盲目消耗 LLM）|
| `/review <fixture_id>` | 对**已结束**的比赛做盘口复盘：实时拉最终比分 + 全程盘口走势 → LLM 事后归因（约 1~3 分钟）|

> **`/analyze` 两步式**：第一步读库展示盘口走势预览 + 拉基本面（不耗 LLM）；
> 末条消息带两个内联按钮——【🎯 预设精算】直接按标准 SOP 跑；【✍️ 自定义侧重】
> 引导你回复一句侧重要求（如「重点看临场异动」「忽略基本面只看盘口」），在不破坏
> SOP 步骤与 `### 1~7` 输出结构的前提下追加到提示词，再跑 gpt-5.5（流式，实时播报 7 步进度）并归档。
> **`/review`** 与 `/analyze` 完全独立：只看盘口走势 + 实际比分，复盘哪些信号准/误导，
> 报告归档为 `report/<日期>/<主队>_vs_<客队>_review.md`。

**安全与两级权限**：bot 只响应 `.env` 白名单里的 chat_id，其他人发消息会被告知
自己的 chat_id 但无法操控。未配置白名单时拒绝所有人。

- `TELEGRAM_ALLOWED_CHAT_IDS` — 能用 bot 的全体（管理员 + 访客）。**访客**只能
  查询与分析：`/fixtures` `/coverage` `/odds` `/export` `/analyze` `/review` `/status`。
- `TELEGRAM_ADMIN_CHAT_IDS` — **管理员**，在访客权限之上额外可改配置：
  `/leagues` `/bookmakers` `/add` `/remove`（及对应的开关按钮）。
- 向后兼容：不配 `TELEGRAM_ADMIN_CHAT_IDS` 时，`ALLOWED` 全员视为管理员（旧行为）。
- 给别人开放用：把对方 chat_id 加进 `TELEGRAM_ALLOWED_CHAT_IDS`（**不要**加进 ADMIN），
  他即成访客——能选比赛跑 `/analyze`、`/review`，但碰不到你的联赛/庄家配置。
  注意：访客跑精算会消耗你的 LLM/API 额度；各人与 bot 的私聊互相独立、互不可见。

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
# 可选：启用 Telegram bot 远程操控
TELEGRAM_BOT_TOKEN=从@BotFather获取的token
TELEGRAM_ALLOWED_CHAT_IDS=你的chat_id,访客的chat_id
# 可选：管理员（能改联赛/庄家配置）。留空则 ALLOWED 全员皆管理员。
# 想给别人开放但只让其查询/精算时：把对方加进 ALLOWED、不加进 ADMIN。
TELEGRAM_ADMIN_CHAT_IDS=你的chat_id
# 可选：启用 /analyze LLM 精算（IKuncode 等 OpenAI 兼容平台）
LLM_BASE_URL=https://api.ikuncode.cc/v1
LLM_API_KEY=你的LLM密钥
EOF
```

> 不需要 Telegram bot 就只填 `APIFOOTBALL_KEY`，守护进程会自动退化为纯调度器模式。
> `odds.db` 会在首次运行时自动创建，无需手动建。

#### 拿 Telegram token 和 chat_id

1. 在 Telegram 找 **@BotFather**，发 `/newbot`，按提示起名，拿到形如 `123456:ABC-DEF...` 的 **token**。
2. 拿你自己的 **chat_id**：先给你新建的 bot 发任意一条消息，然后浏览器打开
   `https://api.telegram.org/bot<你的token>/getUpdates`，找到 `"chat":{"id":数字}`，那个数字就是。
   （或者直接启动守护进程后给 bot 发消息，它会回复告诉你 chat_id。）

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
