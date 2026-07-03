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
  查询与分析：`/fixtures` `/coverage` `/export` `/analyze` `/review` `/status`。
- `TELEGRAM_ADMIN_CHAT_IDS` — **管理员**，在访客权限之上额外可改配置：
  `/leagues` `/bookmakers` `/add` `/remove`（及对应的开关按钮）。
- 向后兼容：不配 `TELEGRAM_ADMIN_CHAT_IDS` 时，`ALLOWED` 全员视为管理员（旧行为）。
- 给别人开放用：把对方 chat_id 加进 `TELEGRAM_ALLOWED_CHAT_IDS`（**不要**加进 ADMIN），
  他即成访客——能选比赛跑 `/analyze`、`/review`，但碰不到你的联赛/庄家配置。
  注意：访客跑精算会消耗你的 LLM/API 额度；各人与 bot 的私聊互相独立、互不可见。

---

## 🎓 访客使用教程（保姆级）

> 这一节是写给**访客**看的：你不需要懂代码、不用碰服务器，只要会用 Telegram 发消息就行。
> 整个流程就两件事——**先挑一场比赛拿到它的编号，再让 bot 帮你分析**。

### 第 0 步：拿到访问权限

1. 你需要有 Telegram 账号，并找到管理员给你的那个 bot（一个 `@xxx_bot` 的链接或用户名）。
2. 点开它，按底部的 **Start / 开始** 按钮，或直接发一句 `/start`。
3. 如果你**还没被授权**，bot 会回一句：

   > ⛔ 未授权。你的 chat_id 是 `123456789`，把它加入服务器 .env 的 TELEGRAM_ALLOWED_CHAT_IDS 即可。
   把这串数字（你的 **chat_id**）发给管理员，请他加进白名单。加好后你再发 `/start`，
   就能看到命令菜单了。这一步只需做一次。

> 你和 bot 的对话是**私密的**，别的访客看不到你发了什么、跑了什么报告；管理员也看不到你的聊天内容，
> 只能在服务器上看到你跑出来的报告文件。

### 第 1 步：打开命令菜单

授权成功后，在聊天框左下角会有一个 **≡ 菜单按钮**，或者你在输入框打一个 `/`，
就会弹出所有可用命令的列表，点一下即可，不用手敲。

随时发 `/help` 可以看到你能用的全部命令说明。

### 第 2 步：挑一场比赛，记下它的编号（fixture_id）

发送：

```
/fixtures
```

bot 会列出**过去 3 天到未来 3 天**的比赛，每行长这样：

```text
🔵 1234567 06-19 03:00  USA vs Australia
✅ 1234560 06-18 22:00  Mexico vs South Africa
```

- 行首那串数字（`1234567`）就是这场比赛的 **fixture_id**，后面所有命令都要用它。
- 🔵 = **未来的比赛**，可以用 `/analyze` 做赛前精算预测。
- ✅ = **已经开赛/结束的比赛**，可以用 `/review` 做赛后复盘。
- 时间都是**北京时间**。

### 第 3 步（核心）：让 bot 分析这场比赛

#### 📊 `/analyze <编号>` — 赛前精算预测（最常用）

对一场**还没开打**的比赛跑分析，例如：

```
/analyze 1234567
```

它分**两步**进行（这样你能先看数据，再决定要不要花时间跑 AI）：

1. **先看数据**：bot 先发来这场的**盘口走势**和**球队基本面**（两队近况、历史交锋、未来赛程、积分榜），
   这一步**不消耗**你的次数。
2. **再选怎么跑**：最后一条消息带两个按钮——
   - **🎯 预设精算**：直接按标准 SOP 流程跑，点了就开始。
   - **✍️ 自定义侧重**：点完后，bot 会等你**回复一句**你的侧重要求
     （比如「重点看临场异动」「忽略基本面只看盘口」），然后按你的要求跑。
     不想跑了就回 `取消`。

   点任一按钮后，AI（gpt-5.5）开始推理，约 **1~3 分钟**，期间会原地显示 7 个步骤的进度条。
   跑完会发来一份完整的精算报告（盘口定性、资金流向、操盘手法、风控验证、最终结论与置信度）。

> ⚠️ **每日次数限制**：访客每天最多跑 **10 次** `/analyze`（管理员不限）。
> 每跑一次成功的精算，bot 会提示「今日剩余 N 次」。次数用完了第二天会自动恢复。
> 只看数据、点开按钮但没真正跑，不算次数。

#### 🔍 `/review <编号>` — 赛后复盘

对一场**已经结束**的比赛做复盘，例如：

```
/review 1234560
```

bot 会先**不告诉 AI 比分**、只给盘口走势让它正向推一遍预判，再揭晓真实比分做对照，
告诉你哪些盘口信号准了、哪些误导了。约 1~3 分钟。同样计入每日次数。

### 第 4 步：辅助查询命令（随时可用，不消耗精算次数）

| 命令 | 怎么用 | 作用 |
|------|--------|------|
| `/coverage <编号>` | `/coverage 1234567` | 看这场**数据采集到什么程度**了：10 个时间节点（初盘→临场）抓到了几个、缺哪些。缺得多说明赛前太早、数据还没攒够，分析会不够准。 |
| `/export <编号>` | `/export 1234567` | 把这场**全部盘口快照导出成 CSV 文件**发给你下载（文件只发给你，不占服务器）。 |
| `/status` | `/status` | 看当前在抓哪些联赛、哪些庄家。 |
| `/help` | `/help` | 列出你能用的所有命令。 |

### 常见疑问

- **「⛔ 该命令仅管理员可用」**：你点到了 `/leagues` `/bookmakers` `/add` `/remove` 这类配置命令，
  访客用不了，这些是管理员调整抓哪些联赛/庄家用的，不影响你做分析。
- **「fixture xxx 暂无盘口数据」**：这场太早或冷门，还没抓到盘口。过一阵再试，或换一场。
- **「未找到 fixture」**：编号打错了，先 `/fixtures` 重新确认行首的数字。
- **报告跑出来太慢**：gpt-5.5 推理较慢，1~3 分钟正常，期间进度条会动，耐心等它跑完即可。
- **想换一场分析**：直接发新的 `/analyze <新编号>` 即可，不用做别的。

### 一句话速记

> `/fixtures` 挑场记编号 → `/analyze 编号` 看数据点按钮跑预测；已结束的用 `/review 编号` 复盘。

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
# 可选：多端点故障转移。主端点=上面的 LLM_BASE_URL/LLM_API_KEY；这里追加备用，
# 逗号或换行分隔，每条 key|base_url|标签（base_url 省略则复用主端点 URL）。
# 一条不通自动切下一条，坏端点触发熔断后冷却自动恢复。
# 管理员在 TG 发 /llm 可测各端点连通性/延迟，并实时改重试/超时/熔断参数（免重启）。
LLM_ENDPOINTS=sk-备用1|,sk-备用2|https://api.other.com/v1|备用商
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

### 改 .env 自动重启（白名单保存即生效）

改 `.env`（尤其是 TG 白名单）后必须重启 bot 才生效，手动 `systemctl restart` 容易忘，导致已授权用户被当未授权访客挡掉。用 `setup_env_watch.sh` 让 systemd 监听 `.env`，保存即自动重启：

```bash
cd ~/odds_bot
bash setup_env_watch.sh          # 安装并自检
bash setup_env_watch.sh --remove # 卸载
```

原理：一个 systemd `path` 单元（`odds-bot-env.path`）盯住 `.env`，文件一变就触发一次性 service `odds-bot-restart.service` 去 `restart odds-bot`。配一次永久生效，换服务器/重装跑一次即可。

- 若发现保存后没自动重启：把 `/etc/systemd/system/odds-bot-env.path` 里的 `PathModified` 改成 `PathChanged`，再 `sudo systemctl daemon-reload && sudo systemctl restart odds-bot-env.path`（vim 改名覆盖写入偶尔只触发 `PathChanged`）。
- 短时间反复存盘被限流报 `start-limit`：`sudo systemctl reset-failed odds-bot`。


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
