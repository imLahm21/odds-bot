<div align="center">

# odds-bot

**Automated Football Odds Polling & Handicap Analysis Backend**

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white)
![API-Football](https://img.shields.io/badge/API--Football-v3-00A859)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-OpenAI--compatible-412991)
![License](https://img.shields.io/badge/License-MIT-yellow)

[English](README.en.md) | [中文](README.md)

</div>

A fully automated polling daemon built on **API-Football**: it periodically scrapes European odds,
Asian handicap, and over/under lines for watched leagues, stores them as a time series in SQLite,
and feeds them to an analysis SOP (see `CLAUDE.md`). Optionally exposes a Telegram bot for remote
control and hooks into any OpenAI-compatible LLM endpoint for pre-match projection and post-match review.

---

## ⚠️ Important Notice

Please read the following carefully before using this project:

- **📚 Educational Purpose Only**: This project is provided for technical learning and research
  purposes only. It is not gambling advice, investment guidance, or a betting recommendation of any kind.
- **⚖️ Compliant Use**: Use this project only in compliance with the laws and regulations of your
  country or region. Any unlawful use is strictly prohibited.
- **🧾 Disclaimer**: The authors assume no liability for any financial loss, account bans, service
  interruptions, data loss, or any other direct or indirect damages resulting from the use of this project.
- **🔑 Third-Party Services**: You are responsible for obtaining and complying with the terms of any
  third-party APIs (API-Football, LLM providers, Telegram, etc.) you configure. All API keys are yours to manage.

---

## Architecture

```
bot/
├── config.py       # Leagues, watched bookmakers, polling intervals, node definitions — edit here to add/remove leagues
├── api_client.py   # API-Football request layer: key rotation + 429/401 retry
├── db.py           # SQLite: schema / bulk insert / WAL / dedup
├── parser.py       # JSON→rows + Kelly index calc (full quarter-ball Asian handicap)
├── scheduler.py    # Scheduled polling tasks
├── llm_client.py   # LLM endpoint failover + circuit breaker
├── tgbot.py        # Telegram bot commands & inline panels
└── daemon.py       # Entry point: init → fetch fixtures → start scheduler
probe*.py           # Stage-0 probes: inspect real API JSON (dev only, not needed in production)
```

### Two database tables

- `fixtures`: fixture metadata (teams, kickoff time, league, status)
- `odds_history`: timestamped odds snapshots (European + Asian handicap + over/under + Kelly),
  used to reconstruct the SOP 10-node timeline

### Scheduled tasks

| Task | Frequency | Action |
|------|-----------|--------|
| A | Daily | Fetch upcoming fixtures for watched leagues → `fixtures` |
| B | Every 1 hour | Scrape latest odds for fixtures in the next few days → `odds_history` |
| C | Every 15 min | High-frequency scrape for near-kickoff fixtures (within 2h) |
| D | Every 5 min | Pre-kickoff sprint window, capturing pre-close movements |

> Watched leagues/bookmakers live in the database and are toggled in real time via the Telegram bot —
> changes take effect immediately, no restart needed.

### Telegram bot (optional)

When `TELEGRAM_BOT_TOKEN` is set, the daemon also runs a TG bot with inline-button controls:

| Command | Purpose |
|---------|---------|
| `/leagues` `/bookmakers` | League / bookmaker toggle panels |
| `/add` `/remove` | Add / remove watched leagues |
| `/status` `/fixtures` | Current config / fixture list |
| `/coverage` `/export` | Data collection progress / export odds CSV |
| `/analyze` | Pre-match projection: preview fundamentals + odds movement, then run SOP (preset or custom focus) |
| `/review` | Post-match review: fetch final score + odds movement → LLM attribution |

**Two-tier permissions**: the bot only responds to whitelisted chat IDs. `TELEGRAM_ALLOWED_CHAT_IDS`
are users who can query/analyze; `TELEGRAM_ADMIN_CHAT_IDS` are admins who can change config. If ADMIN
is unset, all ALLOWED users are treated as admins.

---

## Deployment

```bash
git clone <your-repo-url>
cd odds-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Configure secrets (`.env` is not in git — create it manually):

```bash
cat > .env <<'EOF'
APIFOOTBALL_KEY=your-api-football-key
# Optional: Telegram bot
TELEGRAM_BOT_TOKEN=token-from-@BotFather
TELEGRAM_ALLOWED_CHAT_IDS=your_chat_id,guest_chat_id
TELEGRAM_ADMIN_CHAT_IDS=your_chat_id
# Optional: LLM analysis (any OpenAI-compatible platform)
LLM_BASE_URL=https://<your-openai-compatible-endpoint>/v1
LLM_API_KEY=your-llm-key
EOF
```

> With only `APIFOOTBALL_KEY` set, the daemon runs as a pure scheduler. `odds.db` is created
> automatically on first run. See code comments and the `deploy/` directory for advanced options
> (multi-endpoint failover, circuit-breaker tuning, systemd units, backups).

Start (tmux for keep-alive, or systemd for auto-start — sample unit in `deploy/`):

```bash
tmux new -s bot
source venv/bin/activate
python -m bot.daemon
# Ctrl+b then d to detach
```

---

## Managing leagues

Edit `WATCH_LEAGUES` in `bot/config.py` (key=league_id, value=(name, season)).
Run `python probe.py leagues` to look up a new league's ID.

## Querying data

```bash
sqlite3 odds.db "SELECT count(*) FROM odds_history;"
sqlite3 odds.db "SELECT home_team, away_team, commence_utc FROM fixtures ORDER BY commence_utc LIMIT 10;"
```

---

## License

Released under the MIT License. See `LICENSE` for details.
