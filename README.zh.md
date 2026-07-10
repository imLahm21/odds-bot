<div align="center">

# odds-bot

**足球赔率轮询抓取与精算后台**

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white)
![API-Football](https://img.shields.io/badge/API--Football-v3-00A859)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-OpenAI--compatible-412991)
![License](https://img.shields.io/badge/License-MIT-yellow)

[English](README.md) | [中文](README.zh.md)

</div>

基于 **API-Football** 的全自动轮询守护进程：定时抓取关注联赛的欧赔/亚盘/大小球盘口，
按时间序列存入 SQLite，供精算 SOP（见 `CLAUDE.md`）分析使用。可选挂接 Telegram bot 远程操控，
以及 OpenAI 兼容的 LLM 端点做赛前精算 / 赛后复盘。

---

## ⚠️ 重要声明

使用本项目前请务必阅读：

- **📚 仅供学习研究**：本项目仅用于技术学习与研究，不构成任何博彩建议、投资指导或下注推荐。
- **⚖️ 合规使用**：请在符合你所在国家/地区法律法规的前提下使用，严禁用于任何非法用途。
- **🧾 免责声明**：因使用本项目导致的任何资金损失、账号封禁、服务中断、数据丢失或其它直接/间接损害，作者概不负责。
- **🔑 第三方服务**：你需自行申请并遵守所配置的第三方 API（API-Football、LLM 平台、Telegram 等）的服务条款，所有密钥由你自行管理。

---

## 架构

```
bot/
├── config.py       # 联赛清单、关注庄家、轮询间隔、节点定义——改这里即可增删联赛
├── api_client.py   # API-Football 请求层：key 轮换 + 429/401 重试
├── db.py           # SQLite：建表 / 批量插入 / WAL / 去重
├── parser.py       # JSON→行解析 + 凯利计算（含完整 1/4 亚盘）
├── scheduler.py    # 定时任务调度
├── llm_client.py   # LLM 端点故障转移 + 熔断器
├── tgbot.py        # Telegram bot 命令与内联面板
└── daemon.py       # 入口：初始化→拉赛程→启动调度器
probe*.py           # 阶段0 探针：实测 API 真实 JSON（开发用，部署不需要）
```

### 数据库两张表

- `fixtures`：赛程基本面（对阵、开球时间、联赛、状态）
- `odds_history`：带时间戳的盘口快照（欧赔 + 亚盘 + 大小球 + 凯利），用于重建 SOP 10 节点

### 定时任务

| 任务 | 频率 | 动作 |
|------|------|------|
| A | 每日定时 | 拉关注联赛未来赛程 → `fixtures` |
| B | 每 1 小时 | 抓未来数天比赛最新赔率 → `odds_history` |
| C | 每 15 分钟 | 仅抓临场比赛（开球前 2h 内高频）|
| D | 每 5 分钟 | 开球前冲刺窗口，采封盘前异动 |

> 关注的联赛/庄家存在数据库表中，由 Telegram bot 实时开关，点完即时生效，无需重启。

### Telegram bot（可选）

配置了 `TELEGRAM_BOT_TOKEN` 时，守护进程会同时跑一个 TG bot，用内联按钮实时操控：

| 命令 | 作用 |
|------|------|
| `/leagues` `/bookmakers` | 联赛 / 庄家开关面板 |
| `/add` `/remove` | 新增 / 删除关注联赛 |
| `/status` `/fixtures` | 当前配置 / 赛程列表 |
| `/coverage` `/export` | 某场数据采集进度 / 导出盘口 CSV |
| `/analyze` | 赛前精算：先看基本面+盘口走势，再选预设或自定义侧重跑 SOP |
| `/review` | 赛后复盘：拉最终比分 + 盘口走势 → LLM 事后归因 |

**两级权限**：bot 只响应白名单里的 chat_id。`TELEGRAM_ALLOWED_CHAT_IDS` 为可查询/分析的全体，
`TELEGRAM_ADMIN_CHAT_IDS` 为可改配置的管理员；不配 ADMIN 时 ALLOWED 全员视为管理员。

---

## 部署

```bash
git clone <your-repo-url>
cd odds-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

配置密钥（`.env` 不在 git 里，需手动建）：

```bash
cat > .env <<'EOF'
APIFOOTBALL_KEY=你的API-Football密钥
# 可选：Telegram bot
TELEGRAM_BOT_TOKEN=从@BotFather获取的token
TELEGRAM_ALLOWED_CHAT_IDS=你的chat_id,访客的chat_id
TELEGRAM_ADMIN_CHAT_IDS=你的chat_id
# 可选：LLM 精算（任意 OpenAI 兼容平台）
LLM_BASE_URL=https://<your-openai-compatible-endpoint>/v1
LLM_API_KEY=你的LLM密钥
EOF
```

> 只填 `APIFOOTBALL_KEY` 时，守护进程自动退化为纯调度器模式。`odds.db` 首次运行自动创建。
> 多端点故障转移、熔断参数等进阶配置见代码注释与 `deploy/` 目录。

启动（tmux 保活或 systemd 自启，systemd 单元样例见 `deploy/`）：

```bash
tmux new -s bot
source venv/bin/activate
python -m bot.daemon
# Ctrl+b 然后 d 挂起
```

---

## 增删联赛

改 `bot/config.py` 的 `WATCH_LEAGUES`（key=league_id，value=(中文名, season)）。
需要新联赛 ID 时跑 `python probe.py leagues` 查出。

## 查数据

```bash
sqlite3 odds.db "SELECT count(*) FROM odds_history;"
sqlite3 odds.db "SELECT home_team, away_team, commence_utc FROM fixtures ORDER BY commence_utc LIMIT 10;"
```
